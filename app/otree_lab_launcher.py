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

import datetime as _dt
import getpass
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser

# NEW-feature logic (lab presets, DB create, room enumeration, launch briefing)
# lives once in otree_core and is called from both launchers. The OLD logic in
# this file stays duplicated on purpose (Julian's call); only the four new
# features route through `core`. otree_core is standard-library-only at import
# time (psycopg2 is imported lazily inside core.create_database), so this keeps
# the "stdlib + tkinter only" rule for a normal launch.
import otree_core as core

APP_NAME = "oTree Lab Launcher"
APP_DIR_NAME = "oTreeLabLauncher"
PRESETS_FILENAME = "presets.json"
# A gitignored, one-word per-machine marker ("large"/"small") that identifies
# which lab this computer is. It lives next to the launcher, not in a
# researcher's project, because it is a property of the machine. (Duplicated
# from otree_core on purpose, see CLAUDE.md; the two stay schema-compatible.)
LAB_MARKER_FILENAME = "lab.local"
STORAGE_VERSION = 1

# ---------------------------------------------------------------------------
# Lab-specific data (hosts, seats, database, admin) lives in lab_info.json
# (gitignored) and is loaded by otree_core. NOTHING lab-specific is hardcoded
# here; this launcher reads the values through `core` so there is one source of
# truth. On a first run (lab_info.json absent) the setup wizard writes the file.
# ---------------------------------------------------------------------------

# `core.LAB_DB` is a live module attribute: after the first-run wizard writes
# lab_info.json and calls core.reload_lab_info(), reading core.LAB_DB returns the
# new credentials. normalize_config() reads it directly so it never goes stale.

DB_MODE_LAB = core.DB_MODE_LAB
DB_MODE_CUSTOM = core.DB_MODE_CUSTOM
DB_MODE_NONE = core.DB_MODE_NONE

DB_MODE_LABELS = dict(core.DB_MODE_LABELS)
DB_MODE_BY_LABEL = {v: k for k, v in DB_MODE_LABELS.items()}
# A pseudo-entry in the Setup dropdown: choosing it opens the create-database
# generator directly (so a researcher finds "New database" where databases are
# chosen, without first switching to Custom). It is an action, not a mode.
NEW_DB_LABEL = "New database for this project…"

LAB_CUSTOM = core.LAB_CUSTOM
LAB_LOCAL = core.LAB_LOCAL

AUTH_LEVELS = ["STUDY", "DEMO", "none"]

# The room name is static: it must match the room the shortcuts point at.
DEFAULT_ROOM_NAME = "study"

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
    "db_name": core.LAB_DB["db_name"],
    "db_user": core.LAB_DB["db_user"],
    "db_password": core.LAB_DB["db_password"],
    "db_host": core.LAB_DB["db_host"],
    "db_port": core.LAB_DB["db_port"],
    "admin_username": core.DEFAULT_ADMIN_USERNAME,
    "admin_password": core.DEFAULT_ADMIN_PASSWORD,
    "production": True,
    "auth_level": "STUDY",
    "lab": core.DEFAULT_LAB_ID,
    "custom_host": "",
    "port": "8000",
    "page": "/rooms",
    "resetdb": True,
    "open_browser": True,
    "wait_seconds": 2,
    "room_name": DEFAULT_ROOM_NAME,
    "seat_mode": SEAT_DEFAULT,
    "seat_excluded": [],
    "seat_file": "",
}

# The user-editable settings of a config.  Anything outside this list
# (name, created, last_run, plus keys written by a future version) is
# metadata and is never compared or overwritten.
FIELD_KEYS = tuple(DEFAULT_CONFIG.keys())


def refresh_defaults_from_core():
    """Re-pull the database/admin/lab defaults from core after lab_info.json is
    (re)loaded, used once the first-run wizard has written the file so the app
    picks up the real values without a restart."""
    DEFAULT_CONFIG.update({
        "db_name": core.LAB_DB["db_name"],
        "db_user": core.LAB_DB["db_user"],
        "db_password": core.LAB_DB["db_password"],
        "db_host": core.LAB_DB["db_host"],
        "db_port": core.LAB_DB["db_port"],
        "admin_username": core.DEFAULT_ADMIN_USERNAME,
        "admin_password": core.DEFAULT_ADMIN_PASSWORD,
        "lab": core.DEFAULT_LAB_ID,
    })

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


# ---------------------------------------------------------------------------
# Config helpers (no tkinter here, so the test script can exercise them)
# ---------------------------------------------------------------------------


def now_iso():
    return _dt.datetime.now().replace(microsecond=0).isoformat()


def default_author():
    """The OS login name, used as the default author of a new config.

    getpass.getuser() consults several environment variables and can raise on a
    stripped-down system, so a failure falls back to an empty string rather than
    taking the launcher down.
    """
    try:
        return getpass.getuser()
    except Exception:
        return ""


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
        out.update(core.LAB_DB)
    # `lab` is a lab-preset id (any of the labs defined in lab_info.json, or
    # "custom"); only a blank value falls back to the default. An id that no
    # longer resolves to a lab is handled at resolve time, not rewritten here.
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

    ``ignore`` names fields to exclude; the built-in default uses
    ``ignore=("project_path",)`` so browsing to a project folder (an input to a
    run, not an edit of the config) does not mark it modified. Kept in sync with
    otree_core.configs_differ.
    """
    na = normalize_config(a)
    nb = normalize_config(b)
    for key in ignore:
        na.pop(key, None)
        nb.pop(key, None)
    return na != nb


def build_database_url(cfg):
    """The DATABASE_URL for this config, or None when no database is set.

    The user name and password are percent-encoded (kept byte-identical to
    otree_core.build_database_url), so a password with a URL-special character
    (which the new Create-a-new-database button now allows) cannot break the URL
    or be misparsed by oTree. A password with no such character is unchanged.
    """
    c = normalize_config(cfg)
    if c["db_mode"] == DB_MODE_NONE:
        return None
    from urllib.parse import quote
    return "postgres://{user}:{password}@{host}:{port}/{name}".format(
        user=quote(str(c["db_user"]), safe=""),
        password=quote(str(c["db_password"]), safe=""),
        host=c["db_host"],
        port=c["db_port"],
        name=c["db_name"],
    )


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


def resolve_host(cfg):
    # Delegates to core, which resolves against the labs in lab_info.json.
    return core.resolve_host(cfg)


def build_url(cfg):
    """The page the launcher auto-opens after the server has started.

    Kept in sync with ``otree_core.build_url``: a config still carrying the
    default page opens the CHOSEN room's monitor (arrival board) rather than the
    plain /rooms list; an explicit custom page is respected. Delegates the room
    -> monitor-path decision to core so the empirically-verified path lives in
    one place.
    """
    c = normalize_config(cfg)
    host = resolve_host(c)
    port = c["port"].strip()
    page = c["page"].strip()
    if not host:
        host = "<host>"
    base = "http://" + host
    if port:
        base += ":" + port
    if core._is_default_open_page(page):
        page = core.room_monitor_path(c["room_name"])
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

    # With no participant list the seat variables are left unset, so the block
    # in settings.py stays inert and the project behaves as it always did.
    if c["seat_mode"] == SEAT_NONE or not label_file:
        for key in SEAT_ENV_KEYS:
            env.pop(key, None)
    else:
        env["OTREE_LAB_LABEL_FILE"] = label_file
        env["OTREE_LAB_ROOM_NAME"] = c["room_name"]

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
    if c["seat_mode"] != SEAT_NONE and label_file:
        keys.extend(SEAT_ENV_KEYS)
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


def lab_default_seats(cfg):
    """The seat labels for the lab this config points at.

    A custom host has no known seat list, so it returns an empty list and the
    launcher refuses to build one rather than inventing seats. Delegates to core,
    which reads the seats from lab_info.json.
    """
    return core.lab_default_seats(cfg)


def resolve_seats(cfg):
    """The seat labels this config will hand to oTree, in order.

    Empty for None mode, and for file mode, where the user's own file is used
    as it stands and never re-written.
    """
    c = normalize_config(cfg)
    if c["seat_mode"] in (SEAT_NONE, SEAT_FILE):
        return []
    seats = lab_default_seats(c)
    if c["seat_mode"] == SEAT_EDIT:
        excluded = set(c["seat_excluded"])
        return [seat for seat in seats if seat not in excluded]
    return seats


def effective_seat_mode(cfg):
    """The seat mode a launch will REALLY use, after resolving empties to None.

    Seats are never a hard block (Julian): an absent seat file, a file that
    cannot be read or is empty, or a lab-default/edit selection that resolves to
    no seats all fall back to SEAT_NONE, a valid open-room launch. A chosen file
    that DOES have labels stays SEAT_FILE (so its labels can still be validated).
    Delegates to core so both launchers agree.
    """
    return core.effective_seat_mode(cfg)


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


def seat_summary(cfg, resolved=None):
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
    labels = resolve_seats(c) if resolved is None else resolved
    if not labels:
        # No seats is not an error: it simply becomes the open (none) room.
        return "No seats: the room opens with no seat board (none)."
    return "%d seats" % len(labels)


def prepare_label_file(cfg, config_name="session"):
    """Settle the participant label file for one run.

    Returns (path or None, explanation). A list the launcher owns is written to
    its own config directory; a file the user picked in their project is used
    exactly as it stands and is never copied or rewritten.
    """
    c = normalize_config(cfg)
    # Resolve empties (no file, unreadable/empty file, no default seats) to the
    # open (none) room rather than erroring: seats are never a hard block.
    mode = effective_seat_mode(c)
    if mode == SEAT_NONE:
        return None, "No participant list, so OTREE_LAB_LABEL_FILE is not set."
    if mode == SEAT_FILE:
        path = os.path.abspath(c["seat_file"].strip())
        return path, "Using the project's own seat file, unchanged: %s" % path
    labels = resolve_seats(c)
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

# (a) ROOMS + participant_label_file: when the launcher has written a seat list
#     for this run, expose it as an oTree room so the admin gets the per-seat
#     presence board. Adds nothing if the launcher wrote no seat list.
if _os.environ.get("OTREE_LAB_LABEL_FILE"):
    # The launcher picked a room and wrote a seat list for this run.
    _lab_room = _os.environ.get("OTREE_LAB_ROOM_NAME", "study")

    # ROOMS may not exist yet in this project.
    try:
        ROOMS
    except NameError:
        ROOMS = []

    # Add the room only if the project does not already define it, so a project
    # that has its own room keeps its own settings.
    if not any(r.get("name") == _lab_room for r in ROOMS):
        ROOMS = list(ROOMS) + [dict(name=_lab_room, display_name="oTree lab session")]

    # Point that room at the seat list the launcher wrote. Mutating in place
    # means any other keys the project set on the room survive.
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
#     username box would do nothing. With no variable set this keeps whatever
#     the project already had, or "admin" if it had none, so off the lab it
#     changes nothing.
try:
    _lab_admin_default = ADMIN_USERNAME
except NameError:
    _lab_admin_default = "admin"
ADMIN_USERNAME = _os.environ.get("OTREE_ADMIN_USERNAME", _lab_admin_default)

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


def inspect_settings(project_path):
    """Look for the lab support block in a project's settings.py.

    Returns a dict:
      readable       could settings.py be read at all
      has_block      the start marker is present
      complete       both markers are present
      rooms_after    a top level `ROOMS =` line appears after the block
      rooms_line     the line number of that assignment, or 0
      message        one line of plain language for the GUI
    """
    result = {"readable": False, "has_block": False, "complete": False,
              "rooms_after": False, "rooms_line": 0, "path": settings_path_for(project_path),
              "message": ""}
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
    else:
        result["message"] = "settings.py has the oTree lab support block."
    return result


def append_block(project_path):
    """Back up settings.py, then append the block. Returns (backup, settings)."""
    path = settings_path_for(project_path)
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path + "." + stamp + ".bak"
    shutil.copy2(path, backup)
    separator = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(separator + LAB_BLOCK)
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


def titlecase_app(name):
    """A readable, title-cased app name, e.g. 'public_goods' -> 'Public Goods'."""
    return re.sub(r"[_-]+", " ", name).strip().title()


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


def repo_root():
    """The repo root: the parent of app/ (where data/ lives). data/ is anchored
    here, NOT next to __file__. (Mirror of otree_core.)"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def app_dir():
    """The folder holding the app code files (app/). (Mirror of otree_core.)"""
    return os.path.dirname(os.path.abspath(__file__))


def data_dir():
    """The single folder holding everything the launcher reads and writes:
    lab.local, lab_info.json, presets.json and seats/. It sits at the repo root
    (parent of app/) so updating is "copy the new version over the top, keep your
    data/ folder". (Mirror of otree_core.)"""
    return os.path.join(repo_root(), "data")


def config_dir():
    """The directory where presets.json and seats/ live: the app's data/ folder.
    (Mirror of otree_core.)"""
    return data_dir()


def presets_path():
    override = os.environ.get("OTREE_LAB_LAUNCHER_PRESETS")
    if override:
        return override
    return os.path.join(config_dir(), PRESETS_FILENAME)


# --- Per-machine lab identity (lab.local) ----------------------------------
# Each lab PC carries a gitignored one-word marker file, `lab.local`, next to
# the launcher, holding the id of the lab it is. Read at startup to configure the
# built-in Lab default's lab. It is set from the UI, the first-launch chooser
# and the Lab Settings "which lab is this computer" control, so it never has to
# be hand-edited; write_lab_marker is the first-run (no-clobber) write and
# set_lab_marker is the change-it-later overwrite. (Mirror of otree_core.)


def lab_marker_path():
    """Where lab.local lives. OTREE_LAB_MARKER overrides it (used by tests)."""
    override = os.environ.get("OTREE_LAB_MARKER")
    if override:
        return override
    return os.path.join(data_dir(), LAB_MARKER_FILENAME)


def read_lab_marker(path=None):
    """This machine's lab id, or None when unset.

    Historically one word (`large`/`small`); it now holds ANY lab preset id (a
    lowercase slug), so a machine can be identified as a lab the operator added.
    The stored word is returned as-is (stripped, lower-cased), so large/small
    still resolve to the two built-in labs. Empty/missing means "unset" (first
    launch). (Mirror of otree_core.)
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

    The UI-settable path (first-run chooser + the Lab Settings "which lab is this
    computer" control), so nobody has to hand-edit lab.local. Accepts any
    non-empty id; returns the id written. (Mirror of otree_core.)
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

    Returns True if written, False if refused (a marker already exists), so a
    first-run choice can never silently revert an identified machine. Accepts any
    non-empty id; set_lab_marker is the overwrite path. (Mirror of otree_core.)
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


def apply_lab_marker(presets, marker=_MARKER_UNSET):
    """Point the built-in Lab default's lab at this machine's lab, in place.

    The built-in default is app-owned, so its lab tracks lab.local. User configs
    are never touched. A no-op when the marker is unset (first launch). Any
    non-empty lab id is honoured. (Mirror of otree_core.)
    """
    if marker is _MARKER_UNSET:
        marker = read_lab_marker()
    if not marker:
        return presets
    for preset in presets:
        if is_builtin(preset):
            preset["lab"] = marker
    return presets


def is_builtin(item):
    """True for the app-owned Lab default (builtin flag, or author == "builtin").

    These configs are pinned to the top and cannot be deleted.
    """
    return bool(item.get("builtin")) or str(item.get("author", "")).strip().casefold() == "builtin"


def lab_suffix(lab):
    """The derived, never-stored name suffix for a config's lab. Delegates to
    core, which names the lab from its lab_info.json preset."""
    return core.lab_suffix(lab)


def display_name(preset):
    """The config name as shown to the user.

    The derived lab suffix is appended ONLY to the built-in Lab default;
    researcher-saved configs still store their own lab but show no suffix.
    """
    name = str(preset.get("name", ""))
    if is_builtin(preset):
        return name + lab_suffix(normalize_config(preset)["lab"])
    return name


def default_preset():
    preset = dict(DEFAULT_CONFIG)
    preset["name"] = "Lab default"
    preset["created"] = now_iso()
    preset["last_run"] = None
    # author/builtin are metadata (like name/created/last_run), NOT config fields
    # in FIELD_KEYS, so they never enter the config-equality comparison that
    # guards immutability. The shipped default is app-owned and built in.
    preset["author"] = "builtin"
    preset["builtin"] = True
    # This machine's lab identity (from lab.local) configures the built-in
    # default's lab. Any non-empty id is honoured; unset (first launch) leaves
    # the code default untouched.
    marker = read_lab_marker()
    if marker:
        preset["lab"] = marker
    return preset


def load_store(path=None):
    """Read the presets file.

    Returns (presets, extra) where `presets` is the list of stored records
    exactly as they were written (unknown keys included) and `extra` holds any
    top-level keys of the file this version does not know about.  A file that
    cannot be parsed is moved aside rather than overwritten.
    """
    path = path or presets_path()
    if not os.path.exists(path):
        return [default_preset()], {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        backup = path + ".broken-" + _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            shutil.copy2(path, backup)
        except OSError:
            pass
        return [default_preset()], {}

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
        presets = [default_preset()]
    return presets, extra


def save_store(presets, extra=None, path=None):
    """Write the presets file atomically.

    The new content goes to a temporary file in the same directory, is flushed
    to disk, and only then replaces the old file, so an interrupted write can
    never leave a half-written presets.json behind.
    """
    path = path or presets_path()
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    payload = dict(extra or {})
    payload["version"] = STORAGE_VERSION
    payload["presets"] = presets
    text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False)

    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=folder, prefix=".presets-", suffix=".tmp", delete=False
    )
    tmp_name = handle.name
    try:
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
    return path


def sort_presets(presets):
    """Built-in (Lab default) first, then most recently run, then by name.

    The built-in default is always pinned to the very top regardless of when it
    last ran; everything else falls under the recent-run ordering below it.
    """

    def key(item):
        stamp = item.get("last_run")
        stamp = stamp if isinstance(stamp, str) and stamp else ""
        return (0 if is_builtin(item) else 1,
                0 if stamp else 1, _invert(stamp), str(item.get("name", "")).casefold())

    return sorted(presets, key=key)


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
    # `author`/`builtin` are metadata like name/created/last_run, never compared
    # by normalize_config/configs_differ, so they do not count as config changes.
    preset["author"] = (author if author is not None else default_author()).strip()
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
# Commands
# ---------------------------------------------------------------------------


def resetdb_command():
    """`otree resetdb`, exactly as the batch file ran it."""
    return ["otree", "resetdb"]


def _applescript_string(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def macos_shell_script(cfg, project_path, env_pairs):
    """The shell line that a new macOS Terminal window will run."""
    parts = ["cd " + shlex.quote(project_path)]
    for key, value in env_pairs:
        parts.append("export %s=%s" % (key, shlex.quote(value)))
    parts.append("echo '%s: oTree prodserver. Press Ctrl-C to stop the server.'" % APP_NAME)
    parts.append("otree prodserver")
    return "; ".join(parts)


def build_server_launch(cfg, project_path, env, platform_name=None):
    """How to start `otree prodserver` in a window the researcher can see.

    Returns a dict with the command to run, the platform branch that produced
    it, and the extra Popen arguments that branch needs.  Kept separate from
    the running of it so both branches can be tested off their own platform.
    """
    platform_name = platform_name or sys.platform
    env_pairs = [(key, env[key]) for key in launcher_env_keys(cfg) if key in env]

    if platform_name.startswith("win"):
        # cmd /k keeps the console open after the server stops, so the
        # researcher can still read the traceback that killed it.
        inner = 'title oTree Server ({app}) && otree prodserver'.format(app=APP_NAME)
        return {
            "kind": "windows",
            "cmd": ["cmd", "/k", inner],
            "cwd": project_path,
            "creationflags": CREATE_NEW_CONSOLE,
            "shell": False,
            "description": "new console window: cmd /k otree prodserver",
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
                "cmd": [terminal, "-e", "otree", "prodserver"],
                "cwd": project_path,
                "creationflags": 0,
                "shell": False,
                "description": "new %s window" % terminal,
            }
    return {
        "kind": "linux-background",
        "cmd": ["otree", "prodserver"],
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
        'start "oTree Server" cmd /k otree prodserver',
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
# Look
# ---------------------------------------------------------------------------

import tkinter as tk  # noqa: E402  (kept below the logic so tests import cheaply)
from tkinter import filedialog, messagebox, ttk  # noqa: E402
from tkinter import font as tkfont  # noqa: E402

COLORS = {
    "window": "#eef0f3",
    "sidebar": "#e4e7ec",
    "sidebar_line": "#d2d7de",
    "card": "#ffffff",
    "card_line": "#d7dbe2",
    "text": "#1b2430",
    "muted": "#5d6874",
    "faint": "#8a94a1",
    "accent": "#a4123f",
    "accent_dark": "#7d0e30",
    "accent_soft": "#f7e8ed",
    "ok": "#1a7f4b",
    "ok_soft": "#e7f4ec",
    "warn": "#8a5a00",
    "warn_soft": "#fdf3e0",
    "error": "#b3261e",
    "error_soft": "#fceceb",
    "log_bg": "#1d2430",
    "log_fg": "#d6dce6",
    "selected": "#ffffff",
    "hover": "#dadee4",
    "field_off": "#f2f3f5",
}

PAD = 10


def _pick_family(root, candidates):
    available = set(tkfont.families(root))
    for name in candidates:
        if name in available:
            return name
    return None


# The bust-in-silhouette emoji marks a config's author; the file-folder emoji
# marks its project folder. Each has a plain-BMP fallback that every font can
# draw, so the byline never shows a "tofu" box on a platform without an emoji
# font.
PERSON_EMOJI = "\U0001F464"
PERSON_FALLBACK = "●"          # a filled circle
FOLDER_EMOJI = "\U0001F4C1"
FOLDER_FALLBACK = "▸"          # a small right-pointing triangle


def glyph_or_fallback(emoji, fallback):
    """Return `emoji` where the default font can draw it, else `fallback`.

    Tk shows a "tofu" box for a character the default font cannot draw. There is
    no direct "can you draw this" query, so this compares the measured width of
    the emoji against the width of a code point no normal font defines: if the
    emoji is wider, a real glyph (or the platform's emoji font via font-linking)
    is backing it; otherwise both are the same empty box and the fallback is used.
    """
    try:
        base = tkfont.nametofont("TkDefaultFont")
        if base.measure(emoji) > base.measure("￿"):
            return emoji
    except tk.TclError:
        pass
    return fallback


def final_folder_name(path):
    """The last path segment of a project path, for either OS's separator."""
    cleaned = str(path or "").strip().replace("\\", "/").rstrip("/")
    return cleaned.rsplit("/", 1)[-1] if cleaned else ""


class Fonts(object):
    """Fonts derived from the platform default, so Windows keeps Segoe UI."""

    def __init__(self, root):
        base = tkfont.nametofont("TkDefaultFont")
        self.size = base.actual("size")
        if self.size <= 0:  # negative sizes are pixels; fall back to a point size
            self.size = 10
        family = base.actual("family")
        mono_family = _pick_family(
            root, ("Consolas", "Menlo", "DejaVu Sans Mono", "Courier New")
        ) or tkfont.nametofont("TkFixedFont").actual("family")

        self.body = tkfont.Font(root=root, family=family, size=self.size)
        self.bold = tkfont.Font(root=root, family=family, size=self.size, weight="bold")
        self.small = tkfont.Font(root=root, family=family, size=max(self.size - 1, 8))
        self.small_bold = tkfont.Font(
            root=root, family=family, size=max(self.size - 1, 8), weight="bold"
        )
        self.small_italic = tkfont.Font(
            root=root, family=family, size=max(self.size - 1, 8), slant="italic"
        )
        self.heading = tkfont.Font(root=root, family=family, size=self.size + 4, weight="bold")
        # A smaller heading for the sidebar product name, so it does not compete
        # with the config header on the top row.
        self.subhead = tkfont.Font(root=root, family=family, size=self.size + 1, weight="bold")
        self.card_title = tkfont.Font(root=root, family=family, size=self.size, weight="bold")
        self.mono = tkfont.Font(root=root, family=mono_family, size=max(self.size - 1, 8))
        self.mono_small = tkfont.Font(root=root, family=mono_family, size=max(self.size - 2, 8))
        self.launch = tkfont.Font(root=root, family=family, size=self.size + 2, weight="bold")


def apply_theme(root, fonts):
    style = ttk.Style(root)
    # Prefer the native theme where there is one; clam elsewhere.
    for candidate in ("vista", "aqua", "clam", "default"):
        if candidate in style.theme_names():
            style.theme_use(candidate)
            break

    style.configure(".", font=fonts.body)
    style.configure("TFrame", background=COLORS["card"])
    style.configure("Window.TFrame", background=COLORS["window"])
    style.configure("Card.TFrame", background=COLORS["card"])
    style.configure("TLabel", background=COLORS["card"], foreground=COLORS["text"])
    style.configure("Muted.TLabel", background=COLORS["card"], foreground=COLORS["muted"],
                    font=fonts.small)
    style.configure("Field.TLabel", background=COLORS["card"], foreground=COLORS["muted"])
    style.configure("CardTitle.TLabel", background=COLORS["card"], foreground=COLORS["text"],
                    font=fonts.card_title)
    style.configure("Mono.TLabel", background=COLORS["card"], foreground=COLORS["text"],
                    font=fonts.mono)
    style.configure("TCheckbutton", background=COLORS["card"], foreground=COLORS["text"])
    style.configure("TRadiobutton", background=COLORS["card"], foreground=COLORS["text"])
    style.configure("TEntry", fieldbackground="#ffffff", padding=2)
    style.map(
        "TEntry",
        fieldbackground=[("readonly", COLORS["field_off"]), ("disabled", COLORS["field_off"])],
        foreground=[("disabled", COLORS["faint"])],
    )
    style.configure("TCombobox", padding=2)
    style.map("TCombobox", fieldbackground=[("readonly", "#ffffff")])
    style.configure("TSpinbox", padding=2)
    style.configure("Sidebar.TFrame", background=COLORS["sidebar"])
    style.configure("Sidebar.TLabel", background=COLORS["sidebar"], foreground=COLORS["text"])
    style.configure("SidebarHead.TLabel", background=COLORS["sidebar"],
                    foreground=COLORS["muted"], font=fonts.small_bold)
    style.configure("TSeparator", background=COLORS["card_line"])
    style.configure("Slim.TButton", font=fonts.small, padding=(4, 1))
    return style


class Card(tk.Frame):
    """A white panel with a title, a thin border and comfortable padding."""

    def __init__(self, master, title, subtitle=None, **kwargs):
        tk.Frame.__init__(
            self,
            master,
            bg=COLORS["card"],
            highlightbackground=COLORS["card_line"],
            highlightcolor=COLORS["card_line"],
            highlightthickness=1,
            bd=0,
            **kwargs
        )
        header = tk.Frame(self, bg=COLORS["card"])
        header.pack(fill="x", padx=PAD, pady=(PAD - 3, 1))
        tk.Label(
            header, text=title, bg=COLORS["card"], fg=COLORS["text"], anchor="w"
        ).pack(side="left")
        self._title_widget = header
        if subtitle:
            tk.Label(
                self, text=subtitle, bg=COLORS["card"], fg=COLORS["muted"], anchor="w",
                justify="left",
            ).pack(fill="x", padx=PAD, pady=(0, 2))
        self.body = tk.Frame(self, bg=COLORS["card"])
        self.body.pack(fill="both", expand=True, padx=PAD, pady=(3, PAD - 2))
        self.body.columnconfigure(1, weight=1)

    def style_title(self, font):
        for child in self._title_widget.winfo_children():
            child.configure(font=font)


class StatusLine(tk.Frame):
    """A small coloured strip used for the project check and inline errors."""

    def __init__(self, master, font, **kwargs):
        tk.Frame.__init__(self, master, bg=COLORS["card"], **kwargs)
        self.dot = tk.Label(self, text="", bg=COLORS["card"], fg=COLORS["muted"], font=font)
        self.dot.pack(side="left", padx=(0, 6), anchor="n")
        self.label = tk.Label(
            self, text="", bg=COLORS["card"], fg=COLORS["muted"], font=font,
            anchor="w", justify="left", wraplength=340,
        )
        self.label.pack(side="left", fill="x", expand=True)

    def set(self, level, message, quiet=False):
        palette = {
            "ok": (COLORS["ok"], "●"),
            "warn": (COLORS["warn"], "▲"),
            "error": (COLORS["error"], "■"),
            "muted": (COLORS["muted"], "○"),
        }
        color, glyph = palette.get(level, palette["muted"])
        # A quiet OK recedes to a subtle green check with faint text: it is only
        # reassurance, so it should not read as a prominent line (round 5).
        if quiet and level == "ok":
            self.dot.configure(fg=color, text="✓" if message else "")
            self.label.configure(fg=COLORS["faint"], text=message)
            return
        self.dot.configure(fg=color, text=glyph if message else "")
        self.label.configure(fg=color, text=message)

    def set_wraplength(self, value):
        self.label.configure(wraplength=max(value, 160))


class FlowRow(tk.Frame):
    """A left-aligned row of widgets that wraps as ONE unit when it runs out of
    width (the Tk stand-in for a CSS flex-wrap row).

    pack(side="right") cannot do this: a right-packed widget that no longer fits
    is clipped, or ends up stranded on the right once the row breaks. Here every
    item flows left to right and an item that does not fit moves to the start of
    the next line, left-aligned, so it stays visibly grouped with the rest.

    Items are vertically centred on their line. Two optional behaviours:

      separator=True   a purely visual divider (the middle dots of the oTree
                       admin sentence). It is drawn only BETWEEN two items on the
                       same line and hidden where the line breaks, so a wrapped
                       line never starts or ends with a stray dot.
      push_right=True  while everything fits on one line the item sits at the
                       right edge (like justify-content: space-between); as soon
                       as the row wraps it flows left like any other item.

    Children must be created with this frame as their master. The frame manages
    them with place() and sets its own height, so the caller just grids/packs
    the FlowRow with a horizontal fill.
    """

    def __init__(self, master, bg, row_gap=4):
        tk.Frame.__init__(self, master, bg=bg, width=1, height=1)
        self._items = []
        self._row_gap = row_gap
        self._signature = None
        self.bind("<Configure>", self._relayout)

    def add(self, widget, gap=0, separator=False, push_right=False):
        self._items.append({"widget": widget, "gap": gap, "separator": separator,
                            "push_right": push_right})
        # A child whose text changes (a textvariable label) changes its requested
        # size: lay the row out again. The signature check stops feedback loops.
        widget.bind("<Configure>", self._relayout, add="+")
        self._relayout()
        return widget

    def _relayout(self, _event=None):
        width = self.winfo_width()
        sizes = tuple((item["widget"].winfo_reqwidth(), item["widget"].winfo_reqheight())
                      for item in self._items)
        signature = (width, sizes)
        if signature == self._signature:
            return
        self._signature = signature
        if width <= 1:
            # Not mapped yet: reserve one line so the card does not jump.
            self.configure(height=max([h for _w, h in sizes] or [1]))
            return

        # Break the items into lines. Each line is a list of (item, x).
        lines, line, x, pending = [], [], 0, None
        for item, (w, _h) in zip(self._items, sizes):
            if item["separator"]:
                pending = (item, w)
                continue
            lead = 0
            if line:
                lead = item["gap"]
                if pending:
                    lead += pending[0]["gap"] + pending[1]
            if line and x + lead + w > width:
                lines.append(line)
                line, x, lead = [], 0, 0
                if pending:
                    pending[0]["widget"].place_forget()
                pending = None
            if pending:
                x += pending[0]["gap"]
                line.append((pending[0], x))
                x += pending[1]
                pending = None
            if line:
                x += item["gap"]
            line.append((item, x))
            x += w
        if pending:
            pending[0]["widget"].place_forget()
        if line:
            lines.append(line)

        y = 0
        for index, entries in enumerate(lines):
            height = max(entry["widget"].winfo_reqheight() for entry, _x in entries)
            for entry, left in entries:
                widget = entry["widget"]
                if entry["push_right"] and len(lines) == 1:
                    left = max(left, width - widget.winfo_reqwidth())
                widget.place(x=left, y=y + (height - widget.winfo_reqheight()) // 2)
            y += height + (self._row_gap if index < len(lines) - 1 else 0)
        self.configure(height=max(y, 1))


class Tooltip(object):
    """A plain hover tooltip, used to show a path that does not fit its box."""

    def __init__(self, widget, text_provider, delay=450):
        self.widget = widget
        self.text_provider = text_provider
        self.delay = delay
        self.after_id = None
        self.window = None

    def attach(self, widget):
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self.after_id = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self.after_id is not None:
            try:
                self.widget.after_cancel(self.after_id)
            except tk.TclError:
                pass
            self.after_id = None

    def _show(self):
        text = self.text_provider()
        if not text or self.window is not None:
            return
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        tk.Label(
            self.window, text=text, bg="#2c3440", fg="#f2f5f9", justify="left",
            padx=8, pady=5, wraplength=520, bd=0,
        ).pack()
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.window.wm_geometry("+%d+%d" % (x, y))

    def _hide(self, _event=None):
        self._cancel()
        if self.window is not None:
            self.window.destroy()
            self.window = None


class PathBox(tk.Frame):
    """A read-only path box.

    When the path is wider than the box the front is cut off, so the text is
    prefixed with an ellipsis and the whole path is available as a tooltip and
    on a double click, which copies it.  Without that, two folders whose names
    end the same way look identical here.
    """

    def __init__(self, master, font, variable, placeholder="No folder chosen yet"):
        tk.Frame.__init__(self, master, bg="#ffffff", bd=0, highlightthickness=1,
                          highlightbackground="#9aa4b0", highlightcolor="#9aa4b0")
        self.font = font
        self.var = variable
        self.placeholder = placeholder
        self.truncated = False
        self.label = tk.Label(self, text="", bg="#ffffff", fg=COLORS["text"], font=font,
                              anchor="w", cursor="hand2")
        self.label.pack(fill="both", expand=True, padx=5, pady=3)
        self.bind("<Configure>", lambda _e: self.render())
        variable.trace_add("write", lambda *_a: self.render())
        self.tooltip = Tooltip(self, self._tooltip_text)
        self.tooltip.attach(self.label)
        self.tooltip.attach(self)
        self.label.bind("<Double-Button-1>", self._copy)

    def _tooltip_text(self):
        path = self.var.get()
        if not path:
            return self.placeholder
        return path + "\n(double-click to copy)"

    def _copy(self, _event=None):
        path = self.var.get()
        if path:
            self.clipboard_clear()
            self.clipboard_append(path)

    def render(self):
        text = self.var.get()
        available = max(self.winfo_width() - 14, 40)
        if not text:
            self.truncated = False
            self.label.configure(text=self.placeholder, fg=COLORS["faint"])
            return
        self.label.configure(fg=COLORS["text"])
        if self.font.measure(text) <= available:
            self.truncated = False
            self.label.configure(text=text)
            return
        self.truncated = True
        low, high = 0, len(text)
        while low < high:                      # drop characters from the front
            mid = (low + high) // 2
            if self.font.measure("\u2026" + text[mid:]) <= available:
                high = mid
            else:
                low = mid + 1
        self.label.configure(text="\u2026" + text[low:])


class ScrollFrame(tk.Frame):
    """A vertically scrolling container.

    Used for the settings columns so that larger Windows fonts can never push
    a section out of reach; the scrollbar only appears when it is needed.
    """

    def __init__(self, master, bg):
        tk.Frame.__init__(self, master, bg=bg, bd=0, highlightthickness=0)
        self.canvas = tk.Canvas(self, bg=bg, bd=0, highlightthickness=0)
        self.scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._sync)
        self.canvas.bind("<Configure>", self._resize)
        for widget in (self.canvas, self.inner):
            widget.bind("<MouseWheel>", self._wheel)
            widget.bind("<Button-4>", self._wheel)
            widget.bind("<Button-5>", self._wheel)

    def _on_scroll(self, first, last):
        if float(first) <= 0.0 and float(last) >= 1.0:
            self.scroll.pack_forget()
        else:
            self.scroll.pack(side="right", fill="y")
        self.scroll.set(first, last)

    def _sync(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _resize(self, event):
        self.canvas.itemconfigure(self._window, width=event.width)

    def _wheel(self, event):
        if self.scroll.winfo_ismapped():
            delta = -1 if getattr(event, "num", 0) == 5 or getattr(event, "delta", 0) < 0 else 1
            self.canvas.yview_scroll(-delta, "units")


# ---------------------------------------------------------------------------
# Room-layout seat map (a top-down diagram of the lab)
# ---------------------------------------------------------------------------
#
# Each map is (rows, cols, cells). Grid row 1 is the BACK of the room and the
# last grid row is the FRONT, so the diagram is drawn with the front at the
# bottom. A cell is a dict: r, c (1-based grid position), kind
# ("seat" | "exp" | "wall"), an optional label, and optional colspan/rowspan for
# a spanning wall. The geometry is DATA now: it comes from a lab's map file
# (maps/<name>.json, resolved by core) rather than being hardcoded here. The
# converter core.build_seatmap_from_map() turns a map object + the lab's seat
# list into the (rows, cols, cells) tuple this canvas draws; seat labels are
# filled from the lab's own seats in the order the seat cells appear.


def _grid_seatmap(seats, cols=6):
    """A plain card of all a lab preset's computers, for a preset with no
    bespoke geometry.

    The seeded Small/Large labs keep their real room geometry (above); a lab a
    researcher adds by hand has only a seat list, so it is drawn as a plain
    card listing every computer in natural sorted order (2 before 10, A1 before
    A2 before B1). Returns (rows, cols, cells) or None.
    """
    seats = core.sorted_seats(seats)
    if not seats:
        return None
    cols = max(1, min(cols, len(seats)))
    rows = (len(seats) + cols - 1) // cols
    cells = []
    for i, label in enumerate(seats):
        cells.append({"r": i // cols + 1, "c": i % cols + 1, "kind": "seat", "label": label})
    return rows, cols, cells


class SeatMapCanvas(tk.Canvas):
    """A top-down room diagram drawn on a Tk canvas, sized to fit its width."""

    def __init__(self, master, fonts):
        tk.Canvas.__init__(self, master, bg=COLORS["card"], bd=0,
                           highlightthickness=0, height=1)
        self.fonts = fonts
        self._data = None
        self._excluded = set()
        self._interactive = False
        self._on_toggle = None
        self.bind("<Configure>", lambda _e: self._redraw())

    def show(self, data, excluded=None, interactive=False, on_toggle=None):
        """Draw the map from a precomputed (rows, cols, cells) tuple.

        `data` is None to draw nothing. When `interactive` is true each seat is
        clickable and calls `on_toggle` with its label; seats in `excluded` are
        drawn dimmed either way.
        """
        self._data = data if data else None
        self._excluded = set(excluded or ())
        self._interactive = bool(interactive)
        self._on_toggle = on_toggle
        self._redraw()

    def _redraw(self):
        self.delete("all")
        data = self._data
        if not data:
            self.configure(height=1)
            return
        rows, cols, cells = data
        width = self.winfo_width()
        if width <= 1:
            width = 340
        gap, pad = 5, 2
        maxcell = 46 if cols <= 5 else 38
        cell = (width - 2 * pad - (cols - 1) * gap) / float(cols)
        cell = max(18.0, min(float(maxcell), cell))
        grid_w = cols * cell + (cols - 1) * gap
        grid_h = rows * cell + (rows - 1) * gap
        x0 = max(pad, (width - grid_w) / 2.0)
        y0 = float(pad)
        self.configure(height=int(grid_h + 2 * pad))
        for c in cells:
            self._draw_cell(c, x0, y0, cell, gap)

    def _box(self, c, x0, y0, cell, gap):
        x = x0 + (c["c"] - 1) * (cell + gap)
        y = y0 + (c["r"] - 1) * (cell + gap)
        cspan = c.get("colspan", 1)
        rspan = c.get("rowspan", 1)
        w = cspan * cell + (cspan - 1) * gap
        h = cell if rspan <= 1 else rspan * cell + (int(round(rspan)) - 1) * gap
        return x, y, w, h

    def _draw_cell(self, c, x0, y0, cell, gap):
        x, y, w, h = self._box(c, x0, y0, cell, gap)
        kind = c.get("kind", "seat")
        if kind == "wall":
            self._hatch(x, y, w, h)
            return
        if kind == "exp":
            self._rrect(x, y, w, h, fill=COLORS["card"], outline=COLORS["faint"],
                        dash=(3, 2))
            self.create_text(x + w / 2, y + h / 2, text="EXP", fill=COLORS["muted"],
                             font=self.fonts.mono_small)
            return
        label = c.get("label", "")
        excluded = label in self._excluded
        if excluded:
            fill, outline, textfg = COLORS["field_off"], COLORS["card_line"], COLORS["faint"]
        elif self._interactive:
            # Editable map: full contrast so the seats read as controls.
            fill, outline, textfg = "#ffffff", COLORS["card_line"], COLORS["text"]
        else:
            # Read-only map: lighter, so it reads as a picture, not a form.
            fill, outline, textfg = COLORS["card"], COLORS["faint"], COLORS["muted"]
        tag = "seat_%s" % label
        self._rrect(x, y, w, h, fill=fill, outline=outline, tags=(tag,))
        self.create_text(x + w / 2, y + h / 2, text=label, fill=textfg,
                         font=self.fonts.small, tags=(tag,))
        if self._interactive and label:
            self.tag_bind(tag, "<Button-1>", lambda _e, s=label: self._click(s))

    def _click(self, seat):
        if self._interactive and self._on_toggle is not None:
            self._on_toggle(seat)

    def _hatch(self, x, y, w, h):
        self._rrect(x, y, w, h, fill=COLORS["sidebar"], outline=COLORS["card_line"],
                    dash=(3, 2))
        step, d = 8, 0.0
        while d < w + h:
            x1, y1 = x + max(0.0, d - h), y + min(d, h)
            x2, y2 = x + min(d, w), y + max(0.0, d - w)
            self.create_line(x1, y1, x2, y2, fill="#c4cbd4")
            d += step

    def _rrect(self, x, y, w, h, radius=6, **kw):
        """A plain square-cornered cell. Classic Tk cannot draw true rounded
        corners, so the seat cells use honest square corners (which also read
        cleaner) rather than a smoothed-polygon fake."""
        kw.pop("radius", None)
        return self.create_rectangle(x, y, x + w, y + h, **kw)


# ---------------------------------------------------------------------------
# The application window
# ---------------------------------------------------------------------------


class LauncherApp(object):
    def __init__(self, root, store_path=None):
        self.root = root
        self.store_path = store_path or presets_path()
        self.fonts = Fonts(root)
        self.style = apply_theme(root, self.fonts)
        self.person_glyph = glyph_or_fallback(PERSON_EMOJI, PERSON_FALLBACK)
        self.folder_glyph = glyph_or_fallback(FOLDER_EMOJI, FOLDER_FALLBACK)

        self.presets, self.store_extra = load_store(self.store_path)
        # This machine's lab identity (lab.local) configures the built-in
        # default's lab. Applied in memory to the built-in only; user configs
        # untouched. A no-op on first launch (marker unset).
        apply_lab_marker(self.presets)
        # Lab presets (Feature 4): the lab selector reads these instead of the
        # hardcoded tiles. Seeded from the constants for a fresh store, so the
        # two built-in labs are always present and old configs still resolve.
        self.lab_presets = core.lab_presets_from_store(self.store_extra)
        if not os.path.exists(self.store_path):
            try:
                save_store(self.presets, self.store_extra, self.store_path)
            except OSError:
                pass

        self.selected_index = None       # index into self.presets
        self.loading = False             # suppress change handlers while filling the form
        self.dirty = False
        self.running = False
        self.row_widgets = []
        self._password_entries = []

        root.title(APP_NAME)
        root.configure(bg=COLORS["window"])
        root.geometry("1100x720")
        root.minsize(980, 620)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)

        self._make_variables()
        self._build_sidebar()
        self._build_main()
        self._wire_traces()

        self.refresh_sidebar()
        if self.presets:
            self.select_preset(0, log_it=False)
        else:
            self.load_fields(DEFAULT_CONFIG, None)
        self._prefill_last_project()

        self.log("%s ready. Pick a config on the left, then click Launch." % APP_NAME, "muted")
        root.bind("<Control-Return>", lambda _e: self.launch())
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        # First launch on this machine: ask which lab it is (writes lab.local).
        # Deferred so the main window is drawn behind the modal.
        if read_lab_marker() is None:
            root.after(150, self.prompt_lab_identity)

    # -- variables ---------------------------------------------------------

    def _make_variables(self):
        self.var = {}
        for key, default in DEFAULT_CONFIG.items():
            if isinstance(default, bool):
                self.var[key] = tk.BooleanVar(self.root, value=default)
            elif isinstance(default, int):
                self.var[key] = tk.IntVar(self.root, value=default)
            elif isinstance(default, list):
                self.var[key] = tk.StringVar(self.root, value=json.dumps(default))
            else:
                self.var[key] = tk.StringVar(self.root, value=default)
        self.db_mode_label = tk.StringVar(self.root, value=DB_MODE_LABELS[DB_MODE_LAB])
        self.seat_mode_label = tk.StringVar(self.root, value=SEAT_MODE_LABELS[SEAT_DEFAULT])
        self.seat_count = tk.StringVar(self.root, value="")
        self.seat_list_preview = tk.StringVar(self.root, value="")
        self.seat_excluded = set()
        self.block_state = {}
        self.show_db_password = tk.BooleanVar(self.root, value=False)
        self.show_admin_password = tk.BooleanVar(self.root, value=False)
        self.db_url_preview = tk.StringVar(self.root, value="")
        self.url_preview = tk.StringVar(self.root, value="")

    def _wire_traces(self):
        for key in FIELD_KEYS:
            self.var[key].trace_add("write", self._on_field_change)
        # The database chooser is now a pen-icon menu (_open_db_menu →
        # _choose_db_mode), not a traced combobox, so db_mode_label carries no
        # trace: it stays as a plain mirror of the current mode for display.
        self.seat_mode_label.trace_add("write", self._on_seat_mode_change)
        self.var["lab"].trace_add("write", self._on_lab_change)

    # -- sidebar -----------------------------------------------------------

    def _build_sidebar(self):
        bar = tk.Frame(self.root, bg=COLORS["sidebar"], width=252,
                       highlightbackground=COLORS["sidebar_line"], highlightthickness=0)
        bar.grid(row=0, column=0, sticky="nsew")
        bar.grid_propagate(False)
        bar.rowconfigure(2, weight=1)
        bar.columnconfigure(0, weight=1)
        self.sidebar = bar

        # The product title sits top-left, above the saved-config list.
        title = tk.Frame(bar, bg=COLORS["sidebar"])
        title.grid(row=0, column=0, sticky="ew", padx=PAD + 2, pady=(PAD + 2, 2))
        tk.Label(title, text=APP_NAME, bg=COLORS["sidebar"], fg=COLORS["text"],
                 font=self.fonts.subhead, anchor="w").pack(side="left")
        # Lab Settings: Postgres admin config + lab presets (Feature 4). A small
        # gear near the top-left, next to the product title.
        gear = glyph_or_fallback("⚙", "Set")
        # Sized to clearly fill the full height of the title text next to it.
        gear_font = tkfont.Font(root=self.root, family=self.fonts.body.cget("family"),
                                size=self.fonts.size + 14)
        self.settings_button = tk.Label(
            title, text=gear, bg=COLORS["sidebar"], fg=COLORS["muted"],
            font=gear_font, cursor="hand2")
        self.settings_button.pack(side="right", padx=(6, 0))
        self.settings_button.bind("<Button-1>", lambda _e: self.open_lab_settings())
        Tooltip(self.settings_button, lambda: "Lab Settings").attach(
            self.settings_button)

        # SAVED CONFIGS header: the count, and a small "+" that starts a new
        # blank config. The sidebar is otherwise just the config list; there is
        # no helper text, no Delete button (rows have a hover ✕) and no
        # Save-as-new (that lives in the dirty banner only).
        head = tk.Frame(bar, bg=COLORS["sidebar"])
        head.grid(row=1, column=0, sticky="ew", padx=PAD + 2, pady=(6, 6))
        tk.Label(head, text="SAVED CONFIGS", bg=COLORS["sidebar"], fg=COLORS["muted"],
                 font=self.fonts.small_bold, anchor="w").pack(side="left")
        self.new_button = tk.Label(head, text="＋", bg=COLORS["sidebar"], fg=COLORS["muted"],
                                   font=self.fonts.small_bold, cursor="hand2")
        self.new_button.pack(side="right", padx=(6, 0))
        self.new_button.bind("<Button-1>", lambda _e: self.new_blank())
        Tooltip(self.new_button, lambda: "New / blank config").attach(self.new_button)
        self.count_label = tk.Label(head, text="", bg=COLORS["sidebar"], fg=COLORS["faint"],
                                    font=self.fonts.small, anchor="e")
        self.count_label.pack(side="right")

        self.list_area = ScrollFrame(bar, COLORS["sidebar"])
        self.list_area.grid(row=2, column=0, sticky="nsew", padx=(PAD, 4), pady=(0, PAD))

    def _index_of(self, preset):
        for index, item in enumerate(self.presets):
            if item is preset:
                return index
        return None

    def refresh_sidebar(self):
        selected = self.presets[self.selected_index] if self.selected_index is not None else None
        self.presets = sort_presets(self.presets)
        if selected is not None:
            self.selected_index = self._index_of(selected)

        for widget in self.row_widgets:
            widget.destroy()
        self.row_widgets = []

        parent = self.list_area.inner
        last = len(self.presets) - 1
        for index, preset in enumerate(self.presets):
            is_selected = index == self.selected_index
            bg = COLORS["selected"] if is_selected else COLORS["sidebar"]
            row = tk.Frame(parent, bg=bg, cursor="hand2")
            row.pack(fill="x", pady=1)
            accent = tk.Frame(row, bg=COLORS["accent"] if is_selected else bg, width=3)
            accent.pack(side="left", fill="y")
            text = tk.Frame(row, bg=bg)
            text.pack(side="left", fill="x", expand=True, padx=(8, 6), pady=6)

            # Each entry mirrors the reworked config sidebar:
            #   line 1  config NAME + derived "(Large)"/"(Small)" suffix (+ a
            #           "modified" badge when the selected config is dirty)
            #   line 2  FOLDER basename and AUTHOR on one line; the author drops
            #           to its own line below when the two do not both fit
            #   line 3  "Last run ..." text
            # The suffix is derived from the config's lab at display time, is
            # never stored, and appears ONLY on the built-in Lab default.
            display = display_name(preset)
            title = tk.Frame(text, bg=bg)
            title.pack(fill="x")
            name = tk.Label(title, text=display, bg=bg, fg=COLORS["text"],
                            font=self.fonts.bold if is_selected else self.fonts.body,
                            anchor="w", justify="left")
            name.pack(side="left")
            badge = None
            if is_selected and self.dirty:
                badge = tk.Label(title, text=" modified ", bg=COLORS["warn_soft"],
                                 fg=COLORS["warn"], font=self.fonts.small, anchor="w")
                badge.pack(side="left", padx=(6, 0))

            # The built-in Lab default has NEITHER a folder NOR an author, it
            # only pre-fills settings, so it renders just the name (+ suffix)
            # and the "Last run ..." line. No line2 frame at all, to avoid a
            # visible gap. Researcher configs keep the folder + author line.
            line2 = None
            folder = None
            author = None
            if not is_builtin(preset):
                folder_name = final_folder_name(preset.get("project_path", ""))
                author_name = str(preset.get("author", "")).strip()

                # Line 2: folder + author. An empty folder reads as a greyed
                # italic "empty". Measure both against the row's width; when
                # tight, the author wraps to its own line below.
                line2 = tk.Frame(text, bg=bg)
                line2.pack(fill="x")
                if folder_name:
                    folder_text = "%s %s" % (self.folder_glyph, folder_name)
                    folder = tk.Label(line2, text=folder_text, bg=bg, fg=COLORS["muted"],
                                      font=self.fonts.small, anchor="w")
                else:
                    folder_text = "empty"
                    folder = tk.Label(line2, text=folder_text, bg=bg, fg=COLORS["faint"],
                                      font=self.fonts.small_italic, anchor="w")
                folder.pack(side="left")

                if author_name:
                    author_text = "%s  %s" % (self.person_glyph, author_name)
                    avail = self._sidebar_text_width()
                    fits = (self.fonts.small.measure(folder_text) + 12
                            + self.fonts.small.measure(author_text)) <= avail
                    if fits:
                        author = tk.Label(line2, text=author_text, bg=bg, fg=COLORS["muted"],
                                          font=self.fonts.small, anchor="w")
                        author.pack(side="left", padx=(10, 0))
                    else:
                        author = tk.Label(text, text=author_text, bg=bg, fg=COLORS["muted"],
                                          font=self.fonts.small, anchor="w")
                        author.pack(fill="x")

            when = tk.Label(text, text=format_last_run(preset.get("last_run")), bg=bg,
                            fg=COLORS["muted"], font=self.fonts.small, anchor="w")
            when.pack(fill="x")

            widgets = ([row, accent, text, title, name, when]
                       + ([line2] if line2 else []) + ([folder] if folder else [])
                       + ([badge] if badge else []) + ([author] if author else []))
            for widget in widgets:
                widget.bind("<Button-1>", lambda _e, i=index: self.select_preset(i))

            # A small delete x, revealed on hover, on every deletable row.
            if not is_builtin(preset):
                xbtn = tk.Label(row, text="✕", bg=bg, fg=COLORS["faint"],
                                font=self.fonts.small, cursor="hand2")
                xbtn.bind("<Button-1>",
                          lambda e, p=preset: (self._delete_preset(p), "break")[1])
                xbtn.bind("<Enter>", lambda e, b=xbtn: b.configure(fg=COLORS["accent"]), add="+")
                xbtn.bind("<Leave>", lambda e, b=xbtn: b.configure(fg=COLORS["faint"]), add="+")
                self._bind_row_hover(row, widgets, xbtn)

            self.row_widgets.append(row)

            # A faint hairline BETWEEN rows, centred at ~70% of the row width.
            # None after the last row.
            if index != last:
                sep = tk.Frame(parent, bg=COLORS["sidebar"], height=3)
                sep.pack(fill="x")
                sep.pack_propagate(False)
                line = tk.Frame(sep, bg=COLORS["sidebar_line"], height=1)
                line.place(relx=0.15, rely=0.5, relwidth=0.7, height=1)
                self.row_widgets.append(sep)

        count = len(self.presets)
        self.count_label.configure(text="%d" % count)
        # Deleting is per-row (the hover ✕); the built-in Lab default has none,
        # and _delete_preset refuses it, so there is no bottom Delete button.

    def _bind_row_hover(self, row, widgets, xbtn):
        """Show xbtn while the pointer is anywhere over the row, hide it when it
        leaves the row entirely (children included, without flicker)."""
        def show(_e=None):
            xbtn.place(relx=1.0, rely=0.0, x=-6, y=5, anchor="ne")

        def hide_check():
            try:
                widget = row.winfo_containing(*row.winfo_pointerxy())
            except tk.TclError:
                return
            node = widget
            while node is not None:
                if node is row:
                    return
                node = getattr(node, "master", None)
            try:
                xbtn.place_forget()
            except tk.TclError:
                pass

        def leave(_e=None):
            row.after(40, hide_check)

        for widget in widgets + [xbtn]:
            widget.bind("<Enter>", show, add="+")
            widget.bind("<Leave>", leave, add="+")

    def _sidebar_text_width(self):
        """Best estimate of the pixel width available to a config row's text."""
        try:
            width = self.list_area.inner.winfo_width()
        except Exception:
            width = 0
        if width <= 1:  # not laid out yet
            width = 252
        # Subtract the accent bar (3) and the text frame's horizontal padding.
        return max(width - 3 - 14 - 6, 80)

    # -- main panel --------------------------------------------------------

    def _build_main(self):
        # Row order (top to bottom): header, "modified" banner, the scrolling
        # settings area (takes all the spare height), the Export/Launch button
        # bar, and finally the activity log at the very bottom, the log sits
        # BELOW the buttons on purpose.
        main = tk.Frame(self.root, bg=COLORS["window"])
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)   # the settings area is the only stretcher

        # The product title lives in the sidebar; this header names the config
        # currently on screen, e.g. "Config: Lab default" (no quotes).
        header = tk.Frame(main, bg=COLORS["window"])
        header.grid(row=0, column=0, sticky="ew", padx=PAD + 4, pady=(PAD - 2, 2))
        self.subtitle = tk.Label(
            header, text="", bg=COLORS["window"], fg=COLORS["text"],
            font=self.fonts.heading, anchor="w")
        self.subtitle.pack(side="left")

        # A gentle, POSITIVE Save-as-new affordance (not the old alarm banner):
        # it invites saving the current setup as a named config. It appears once a
        # project folder is chosen on the built-in default, or when a config has
        # been changed, and it is styled as a calm neutral strip, never a warning.
        self.banner = tk.Frame(main, bg=COLORS["card"],
                               highlightbackground=COLORS["card_line"], highlightthickness=1)
        self.banner.columnconfigure(0, weight=1)
        self.banner_label = tk.Label(
            self.banner, text="", bg=COLORS["card"], fg=COLORS["muted"],
            font=self.fonts.small, anchor="w", justify="left")
        self.banner_label.grid(row=0, column=0, sticky="ew", padx=10, pady=4)
        ttk.Button(self.banner, text="Save as new config...", style="Slim.TButton",
                   command=self.save_as_new).grid(row=0, column=1, padx=(8, 6), pady=4)
        self.banner.bind("<Configure>", lambda e: self.banner_label.configure(
            wraplength=max(e.width - 170, 220)))

        settings = ScrollFrame(main, COLORS["window"])
        settings.grid(row=2, column=0, sticky="nsew", padx=PAD + 4, pady=(4, 0))
        self.settings_pane = settings
        self._build_cards(settings.inner)

        self._build_bottom(main)          # row 3: the Export/Launch button bar

        log_frame = self._build_log(main)
        log_frame.grid(row=4, column=0, sticky="ew", padx=PAD + 4, pady=(0, PAD - 2))

    def _build_cards(self, parent):
        parent.columnconfigure(0, weight=6, uniform="cols")
        parent.columnconfigure(1, weight=5, uniform="cols")

        left = tk.Frame(parent, bg=COLORS["window"])
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        left.columnconfigure(0, weight=1)
        right = tk.Frame(parent, bg=COLORS["window"])
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        right.columnconfigure(0, weight=1)

        # Left column holds sections 1 (project + admin + reset) and 2
        # (database); the right column holds section 3 (lab + participant list
        # + room-layout map).
        self._card_project(left)
        self._card_database(left)
        self._card_lab(right)

    def _label(self, parent, text):
        return tk.Label(parent, text=text, bg=COLORS["card"], fg=COLORS["muted"],
                        font=self.fonts.body, anchor="w")

    def _field(self, body, row, text, widget, pady=(0, 4)):
        self._label(body, text).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=pady)
        widget.grid(row=row, column=1, sticky="ew", pady=pady)
        return widget

    def _password_row(self, body, row, text, var, show_var):
        holder = tk.Frame(body, bg=COLORS["card"])
        holder.columnconfigure(0, weight=1)
        entry = ttk.Entry(holder, textvariable=var, show=MASK_CHAR)
        entry.grid(row=0, column=0, sticky="ew")
        toggle = ttk.Checkbutton(
            holder, text="Show", variable=show_var,
            command=lambda: entry.configure(show="" if show_var.get() else MASK_CHAR))
        self._password_entries.append((entry, show_var))
        toggle.grid(row=0, column=1, padx=(8, 0))
        self._field(body, row, text, holder)
        return entry

    # (1) oTree project ----------------------------------------------------

    def _card_project(self, parent):
        card = Card(parent, "1.  oTree project")
        card.style_title(self.fonts.card_title)
        card.grid(row=0, column=0, sticky="ew", pady=(0, PAD - 3))
        body = card.body
        body.columnconfigure(0, weight=1)

        picker = tk.Frame(body, bg=COLORS["card"])
        picker.grid(row=0, column=0, columnspan=2, sticky="ew")
        picker.columnconfigure(0, weight=1)
        self.path_box = PathBox(picker, self.fonts.body, self.var["project_path"])
        self.path_box.grid(row=0, column=0, sticky="ew", ipady=1)
        ttk.Button(picker, text="Browse...", command=self.browse_project).grid(
            row=0, column=1, padx=(8, 0))

        # The validation summary line (dot + one sentence)...
        self.project_status = StatusLine(body, self.fonts.small)
        self.project_status.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        # ...and, when the folder is a real oTree project, the app packages as a
        # title-cased bullet list beneath it.
        self.app_bullets = tk.Frame(body, bg=COLORS["card"])
        self.app_bullets.grid(row=2, column=0, columnspan=2, sticky="ew", padx=(20, 0))
        self.app_bullets.grid_remove()

        # A thin divider, then the merged "oTree admin" sub-section.
        tk.Frame(body, bg=COLORS["card_line"], height=1).grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=(10, 8))

        # The admin row is ONE inline sentence of three separate decisions, split
        # by vertically-centred middle dots (UI round 2):
        #
        #     oTree admin ✎   •   Authentication level: STUDY ▾   •   Auto login [x]
        #
        #  - the pen right after "oTree admin" opens/closes the admin username +
        #    password fields below the row (collapsed by default, the same
        #    collapsed-detail pattern as the database pen);
        #  - "Authentication level: STUDY ▾" reads as a sentence: label, colon, the
        #    current value in bold at the SAME font size, then a small chevron.
        #    Clicking it reveals the existing levels as radio options below;
        #  - Auto login (default ON) makes the launcher log the dashboard in for
        #    the operator with a real form-login + one-shot localhost cookie relay
        #    (core.open_dashboard_authenticated) so it lands already logged in; off
        #    (or if the login fails) it opens the plain login page and the operator
        #    logs in by hand (the launch briefing still shows the credentials with
        #    Copy as the manual fallback).
        #
        # It is a FlowRow, so on a narrow window the sentence wraps at a dot (the
        # dot at the break is hidden) instead of being clipped.
        card_bg = COLORS["card"]
        header = FlowRow(body, card_bg)
        header.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        self.admin_sentence = header

        def middle_dot():
            return tk.Label(header, text="•", bg=card_bg, fg=COLORS["muted"],
                            font=self.fonts.bold, bd=0, padx=0, pady=0)

        # (a) oTree admin + pen.
        admin_part = tk.Frame(header, bg=card_bg)
        tk.Label(admin_part, text="oTree admin", bg=card_bg, fg=COLORS["muted"],
                 font=self.fonts.small, anchor="w").pack(side="left")
        self.admin_edit_link = tk.Label(admin_part, text=" ✎", bg=card_bg,
                                        fg=COLORS["accent"], font=self.fonts.small_bold,
                                        cursor="hand2")
        self.admin_edit_link.pack(side="left")
        self.admin_edit_link.bind("<Button-1>", lambda _e: self._toggle_admin_edit())
        Tooltip(self.admin_edit_link,
                lambda: "Edit the oTree admin username and password").attach(
            self.admin_edit_link)
        header.add(admin_part)
        header.add(middle_dot(), gap=10, separator=True)

        # (b) Authentication level: VALUE chevron. It is authentication, so it
        # belongs with the login controls, not with the study-run settings below.
        # The whole phrase is one click target. The value uses the strongest text
        # colour of this (light) theme, bold, at the label's own font size, and
        # sits one space after the colon.
        auth_part = tk.Frame(header, bg=card_bg, cursor="hand2")
        auth_label = tk.Label(auth_part, text="Authentication level:", bg=card_bg,
                              fg=COLORS["muted"], font=self.fonts.small, padx=0,
                              cursor="hand2")
        auth_label.pack(side="left")
        self.auth_value_label = tk.Label(
            auth_part, textvariable=self.var["auth_level"], bg=card_bg,
            fg=COLORS["text"], font=self.fonts.small_bold, padx=0, cursor="hand2")
        self.auth_value_label.pack(side="left", padx=(3, 0))
        # Plain small triangles (U+25BE closed, U+25B4 open): they render
        # reliably in the default fonts on Windows and macOS, unlike U+2304.
        self._auth_chevrons = ("▾", "▴")
        self.auth_chevron = tk.Label(auth_part, text=self._auth_chevrons[0], bg=card_bg,
                                     fg=COLORS["muted"], font=self.fonts.small_bold,
                                     padx=0, cursor="hand2")
        self.auth_chevron.pack(side="left", padx=(3, 0))
        for widget in (auth_part, auth_label, self.auth_value_label, self.auth_chevron):
            widget.bind("<Button-1>", lambda _e: self._toggle_auth_options())
        header.add(auth_part, gap=10)
        header.add(middle_dot(), gap=10, separator=True)

        # (c) Auto login: unchanged, a plain tickbox.
        self.auto_login = tk.BooleanVar(self.root, value=True)
        header.add(tk.Checkbutton(
            header, text="Auto login", variable=self.auto_login,
            bg=card_bg, fg=COLORS["text"], activebackground=card_bg,
            activeforeground=COLORS["text"], selectcolor=COLORS["accent"],
            font=self.fonts.small, anchor="w", bd=0, highlightthickness=0,
            padx=0, cursor="hand2"), gap=10)

        # The two collapsed details sit directly in the card body (rows 5 and 6),
        # NOT in a wrapper frame: a Tk frame whose last child is grid_remove()d
        # keeps its old height, which would leave a blank gap after collapsing.
        # The authentication levels, revealed by the chevron (collapsed by
        # default). Same set as before: STUDY / DEMO / none.
        self.auth_options = tk.Frame(body, bg=card_bg)
        self.auth_options.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(2, 4))
        for level in AUTH_LEVELS:
            tk.Radiobutton(
                self.auth_options, text=level, value=level,
                variable=self.var["auth_level"], command=self._on_auth_level_picked,
                bg=card_bg, fg=COLORS["text"], activebackground=card_bg,
                activeforeground=COLORS["text"], selectcolor=card_bg,
                font=self.fonts.small, bd=0, highlightthickness=0, padx=0,
                cursor="hand2").pack(side="left", padx=(0, 16))
        self._auth_shown = False
        self._set_auth_options(False)
        # Collapsible credential fields (username + masked password with Show).
        self.admin_creds = tk.Frame(body, bg=card_bg)
        self.admin_creds.grid(row=6, column=0, columnspan=2, sticky="ew")
        self.admin_creds.columnconfigure(1, weight=1)
        self._field(self.admin_creds, 0, "Admin username",
                    ttk.Entry(self.admin_creds, textvariable=self.var["admin_username"]))
        self.admin_password_entry = self._password_row(
            self.admin_creds, 1, "Admin password", self.var["admin_password"],
            self.show_admin_password)
        # Collapsed by default; opened when the config has non-default creds.
        self._admin_shown = False
        self._apply_admin_visibility()

        # -- Study settings: the study-run behaviour, under its own heading at the
        #    BOTTOM of the card. Production mode is a run setting (serve as a real
        #    study, no debug pages), NOT authentication, so it lives here; the
        #    authentication level sits on the oTree admin row above instead.
        tk.Frame(body, bg=COLORS["card_line"], height=1).grid(
            row=7, column=0, columnspan=2, sticky="ew", pady=(10, 8))
        tk.Label(body, text="Study settings:", bg=COLORS["card"], fg=COLORS["text"],
                 font=self.fonts.small_bold, anchor="w").grid(
            row=8, column=0, columnspan=2, sticky="w", pady=(0, 4))
        study = tk.Frame(body, bg=COLORS["card"])
        study.grid(row=9, column=0, columnspan=2, sticky="ew")
        tk.Checkbutton(study, text="Production mode (serve as a real study, no debug pages)",
                       variable=self.var["production"], bg=COLORS["card"], fg=COLORS["text"],
                       activebackground=COLORS["card"], activeforeground=COLORS["text"],
                       selectcolor=COLORS["accent"], font=self.fonts.body, anchor="w",
                       bd=0, highlightthickness=0, padx=0, cursor="hand2").pack(anchor="w")

        # (The "Reset the database before starting" checkbox now lives in the
        # Database card, next to the database it resets.)

        body.bind("<Configure>",
                  lambda e: self.project_status.set_wraplength(e.width - 24))

    def _admin_is_default(self):
        """True when the admin username and password are the shipped defaults, so
        the credential fields can stay collapsed without hiding anything unusual."""
        try:
            return (self.var["admin_username"].get() == DEFAULT_CONFIG["admin_username"]
                    and self.var["admin_password"].get() == DEFAULT_CONFIG["admin_password"])
        except tk.TclError:
            return True

    def _set_admin_edit(self, shown):
        """Show/hide the admin username + password fields (the admin pen)."""
        self._admin_shown = bool(shown)
        if not hasattr(self, "admin_creds"):
            return
        if self._admin_shown:
            self.admin_creds.grid()
        else:
            self.admin_creds.grid_remove()

    def _set_auth_options(self, shown):
        """Show/hide the authentication-level options (the chevron)."""
        self._auth_shown = bool(shown)
        if not hasattr(self, "auth_options"):
            return
        if self._auth_shown:
            self.auth_options.grid()
        else:
            self.auth_options.grid_remove()
        self.auth_chevron.configure(text=self._auth_chevrons[1 if self._auth_shown else 0])

    def _toggle_auth_options(self):
        self._set_auth_options(not getattr(self, "_auth_shown", False))

    def _on_auth_level_picked(self):
        # Picking a level closes the options again, like the dropdown this
        # replaces: the sentence above now states the new value.
        self._set_auth_options(False)

    def _toggle_admin_edit(self):
        self._set_admin_edit(not getattr(self, "_admin_shown", False))

    def _apply_admin_visibility(self):
        """Collapse the credential fields by default; auto-expand them when the
        config carries a non-default admin username or password, so an unusual
        value is never hidden."""
        self._set_admin_edit(not self._admin_is_default())

    def _set_app_bullets(self, apps):
        """Show the project's app packages as a title-cased bullet list."""
        for child in list(self.app_bullets.winfo_children()):
            child.destroy()
        if not apps:
            self.app_bullets.grid_remove()
            return
        for app_name in apps:
            tk.Label(self.app_bullets, text="•  " + titlecase_app(app_name),
                     bg=COLORS["card"], fg=COLORS["text"], font=self.fonts.small,
                     anchor="w", justify="left").pack(fill="x")
        self.app_bullets.grid()

    # (2) Database ---------------------------------------------------------

    def _card_database(self, parent):
        card = Card(parent, "2.  Database")
        card.style_title(self.fonts.card_title)
        card.grid(row=1, column=0, sticky="ew", pady=(0, PAD - 3))
        body = card.body
        body.columnconfigure(0, weight=1)
        self.db_summary = tk.StringVar(self.root, value="")

        # One summary line: the current database NAME in bold + a pen (edit) icon
        # that opens a dropdown of databases (lab / oTree default / the current
        # custom / "Add new database…"), with the "Reset before start" tickbox
        # always visible on the same line. There is no "Database" word in front of
        # the name: the card is already titled Database (UI round 2). The full
        # connection fields (below) appear only when the database is a custom one,
        # and are editable there.
        #
        # The line is a FlowRow so the name and the tickbox behave as ONE row:
        # while both fit, the tickbox sits at the right edge; when the window is
        # too narrow it wraps under the name, LEFT-aligned, instead of being
        # clipped or stranded on the right.
        summary_row = FlowRow(body, COLORS["card"])
        summary_row.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.db_summary_row = summary_row

        left = tk.Frame(summary_row, bg=COLORS["card"])
        tk.Label(left, textvariable=self.db_summary, bg=COLORS["card"],
                 fg=COLORS["text"], font=self.fonts.small_bold, anchor="w").pack(side="left")
        # Pen/edit icon: opens the database dropdown. A plain label reads as a
        # button here (ttk buttons look heavy inline); the hand cursor + accent
        # colour signal it is clickable.
        self.db_edit_icon = tk.Label(
            left, text="  ✎", bg=COLORS["card"], fg=COLORS["accent"],
            font=self.fonts.small_bold, cursor="hand2")
        self.db_edit_icon.pack(side="left")
        self.db_edit_icon.bind("<Button-1>", self._open_db_menu)
        summary_row.add(left)

        # Reset-before-start: always visible on this line (no expander), reading
        # plainly. It resets whichever database is shown to the left.
        self.reset_check = tk.Checkbutton(
            summary_row, text="Reset before start",
            variable=self.var["resetdb"], bg=COLORS["card"], fg=COLORS["text"],
            activebackground=COLORS["card"], activeforeground=COLORS["text"],
            selectcolor=COLORS["accent"], font=self.fonts.body, anchor="w",
            bd=0, highlightthickness=0, padx=0, cursor="hand2")
        summary_row.add(self.reset_check, gap=18, push_right=True)

        # The connection fields, shown (and editable) ONLY for a custom database;
        # apply_db_mode grids this in for a custom setup and removes it otherwise.
        self.db_section = tk.Frame(body, bg=COLORS["card"])
        self.db_section.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.db_section.columnconfigure(1, weight=1)
        details = self.db_section
        details.columnconfigure(1, weight=1)
        self.db_details = details

        self.db_entries = {}
        self.db_entries["db_name"] = self._field(
            details, 0, "Database name", ttk.Entry(details, textvariable=self.var["db_name"]))
        self.db_entries["db_user"] = self._field(
            details, 1, "User", ttk.Entry(details, textvariable=self.var["db_user"]))
        self.db_entries["db_password"] = self._password_row(
            details, 2, "Password", self.var["db_password"], self.show_db_password)
        hostport = tk.Frame(details, bg=COLORS["card"])
        hostport.columnconfigure(0, weight=1)
        self.db_entries["db_host"] = ttk.Entry(hostport, textvariable=self.var["db_host"])
        self.db_entries["db_host"].grid(row=0, column=0, sticky="ew")
        tk.Label(hostport, text="Port", bg=COLORS["card"], fg=COLORS["muted"]).grid(
            row=0, column=1, padx=(12, 8))
        self.db_entries["db_port"] = ttk.Entry(hostport, textvariable=self.var["db_port"],
                                               width=8)
        self.db_entries["db_port"].grid(row=0, column=2, sticky="w")
        self._field(details, 3, "Host", hostport)

        preview = tk.Frame(details, bg=COLORS["field_off"], highlightthickness=1,
                           highlightbackground=COLORS["card_line"])
        preview.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        preview.columnconfigure(0, weight=1)
        tk.Label(preview, text="DATABASE_URL", bg=COLORS["field_off"], fg=COLORS["faint"],
                 font=self.fonts.small, anchor="w").grid(
            row=0, column=0, sticky="ew", padx=6, pady=(4, 0))
        self.db_url_label = tk.Label(
            preview, textvariable=self.db_url_preview, bg=COLORS["field_off"],
            fg=COLORS["text"], font=self.fonts.mono_small, anchor="w", justify="left")
        self.db_url_label.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 5))
        preview.bind("<Configure>",
                     lambda e: self.db_url_label.configure(wraplength=max(e.width - 14, 160)))

        # Start on the one-line summary; apply_db_mode reveals the fields for a
        # custom database (and hides them otherwise).
        self.db_section.grid_remove()
        self._refresh_db_summary()

    # (3) Lab (lab + link + participant list + room-layout map) ------------

    def _card_lab(self, parent):
        card = Card(parent, "3.  Lab")
        card.style_title(self.fonts.card_title)
        card.grid(row=0, column=0, sticky="ew", pady=(0, PAD - 3))
        body = card.body
        body.columnconfigure(0, weight=1)

        # -- selectable lab tiles, one per DISPLAYED preset (Feature 4). Built
        #    from core.selectable_lab_presets so a lab a researcher adds on the
        #    Lab Settings page appears here; the seeded Small/Large labs are the
        #    default two. A "Lab" word label sits to their left so the card reads
        #    Lab / Room / Participant list down the rows.
        labrow = tk.Frame(body, bg=COLORS["card"])
        labrow.grid(row=0, column=0, columnspan=2, sticky="ew")
        labrow.columnconfigure(0, weight=1)
        # No "Lab" row-label in front of the tiles: the "3. Lab" section header
        # already labels this row and the tiles are obviously labs, so a second
        # "Lab" word here is redundant (round 7). Room and Participant list keep
        # their row labels; only this one is dropped. The tiles left-align to the
        # card edge, in line with the "Room" / "Participant list" labels below.
        tiles = tk.Frame(labrow, bg=COLORS["card"])
        tiles.grid(row=0, column=0, sticky="ew")
        self.lab_tiles_frame = tiles
        self.lab_tiles = {}
        self._rebuild_lab_tiles()

        # -- Host buttons (UI round 2): "localhost" and "Other host…" are two
        #    adjacent, left-aligned selectable buttons that are there FROM THE
        #    START and in every state, under the lab tiles. The chosen one shows
        #    the same selected look as a chosen lab tile; with a lab chosen,
        #    neither is selected. Choosing "Other host…" reveals the custom host
        #    fields and keeps "localhost" right there to switch to (and back).
        #    While either is chosen the lab tiles fade, and a quiet "← Back to a
        #    lab" link follows the buttons (the only way back on a single-lab
        #    machine, which has no tiles to click).
        self.host_links = tk.Frame(body, bg=COLORS["card"])
        self.host_links.grid(row=1, column=0, columnspan=2, sticky="w", pady=(9, 0))
        self.host_buttons = {}
        for value, text in ((LAB_LOCAL, "localhost"), (LAB_CUSTOM, "Other host…")):
            self.host_buttons[value] = self._make_host_button(self.host_links, value, text)
        self.back_to_lab_link = tk.Label(
            self.host_links, text="← Back to a lab", bg=COLORS["card"], fg=COLORS["muted"],
            font=self.fonts.small, anchor="w", cursor="hand2")
        self.back_to_lab_link.bind(
            "<Button-1>",
            lambda _e: self.var["lab"].set(core.default_selected_lab(self.lab_presets)))

        # A one-line note shown only for the Local (this computer) host, so the
        # tester sees what it does: run on localhost with the oTree default
        # (SQLite) database, no lab infrastructure needed.
        self.local_note = tk.Label(
            body, bg=COLORS["card"], fg=COLORS["muted"], font=self.fonts.small,
            anchor="w", justify="left",
            text=("Runs oTree on this computer (localhost). Pair it with the "
                  "“No lab database (oTree SQLite)” database for a "
                  "self-contained off-lab test."))
        self.local_note.grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.local_note.grid_remove()

        # Host/Port/Page/Room + OPENS link, revealed only for a custom host.
        self.custom_fields = tk.Frame(body, bg=COLORS["card"])
        self.custom_fields.columnconfigure(1, weight=1)
        cf = self.custom_fields
        # Just the address box: localhost is its own button above now, so there
        # is no "use this computer (localhost)" text link here any more.
        hostrow = tk.Frame(cf, bg=COLORS["card"])
        hostrow.columnconfigure(0, weight=1)
        self.custom_host_entry = ttk.Entry(hostrow, textvariable=self.var["custom_host"])
        self.custom_host_entry.grid(row=0, column=0, sticky="ew")
        self._field(cf, 0, "Host", hostrow)
        self.room_entry = ttk.Entry(cf, textvariable=self.var["room_name"])
        self._field(cf, 1, "Room name", self.room_entry)
        # Port and Page are almost never changed, so fold them behind a link.
        self._custom_more_shown = False
        self.custom_more_link = tk.Label(cf, text="▸  Port and page", bg=COLORS["card"],
                                         fg=COLORS["muted"], font=self.fonts.small,
                                         anchor="w", cursor="hand2")
        self.custom_more_link.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.custom_more_link.bind("<Button-1>", lambda _e: self._toggle_custom_more())
        self.custom_more = tk.Frame(cf, bg=COLORS["card"])
        self.custom_more.columnconfigure(1, weight=1)
        self.custom_more.grid(row=3, column=0, columnspan=2, sticky="ew")
        self._field(self.custom_more, 0, "Port",
                    ttk.Entry(self.custom_more, textvariable=self.var["port"]))
        self._field(self.custom_more, 1, "Page to open",
                    ttk.Entry(self.custom_more, textvariable=self.var["page"]))
        self.custom_more.grid_remove()
        preview = tk.Frame(cf, bg=COLORS["field_off"], highlightthickness=1,
                           highlightbackground=COLORS["card_line"])
        preview.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        preview.columnconfigure(0, weight=1)
        tk.Label(preview, text="OPENS", bg=COLORS["field_off"], fg=COLORS["faint"],
                 font=self.fonts.small, anchor="w").grid(
            row=0, column=0, sticky="ew", padx=6, pady=(4, 0))
        self.url_label = tk.Label(preview, textvariable=self.url_preview,
                                  bg=COLORS["field_off"], fg=COLORS["text"],
                                  font=self.fonts.mono_small, anchor="w", justify="left")
        self.url_label.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 5))
        preview.bind("<Configure>",
                     lambda e: self.url_label.configure(wraplength=max(e.width - 14, 160)))

        # -- Room row (Feature 3): normal labs pin to the study room the desktop
        #    shortcuts open, with a "Change room" picker that reads the project's
        #    own ROOMS. Hidden for a custom host, which has its own Room name box.
        self.room_row = tk.Frame(body, bg=COLORS["card"])
        self.room_row.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        tk.Label(self.room_row, text="Room", bg=COLORS["card"], fg=COLORS["muted"],
                 font=self.fonts.body).pack(side="left", padx=(0, 10))
        self.room_value_label = tk.Label(
            self.room_row, textvariable=self.var["room_name"], bg=COLORS["card"],
            fg=COLORS["text"], font=self.fonts.small_bold, anchor="w")
        self.room_value_label.pack(side="left")
        # An unbold, muted "(lab default)" tag after the name, shown only when the
        # room is the study room the desktop shortcuts point at, so it reads
        # "study (lab default)".
        self.room_default_tag = tk.Label(
            self.room_row, text=" (lab default)", bg=COLORS["card"],
            fg=COLORS["muted"], font=self.fonts.small, anchor="w")
        self.room_default_tag.pack(side="left")
        self.var["room_name"].trace_add("write", lambda *_a: self._update_room_default_tag())
        self._update_room_default_tag()
        ttk.Button(self.room_row, text="Change room...", style="Slim.TButton",
                   command=self.pick_room).pack(side="left", padx=(10, 0))

        # -- Participant list row (dropdown only) ------------------------------
        # The seat count is not repeated here (round 7): it lives in the Room
        # layout caption ("Room layout - N seats"), which shows whether the
        # layout is expanded or collapsed.
        partrow = tk.Frame(body, bg=COLORS["card"])
        partrow.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        tk.Label(partrow, text="Participant list", bg=COLORS["card"], fg=COLORS["muted"],
                 font=self.fonts.body).pack(side="left", padx=(0, 10))
        self.seat_mode_box = ttk.Combobox(
            partrow, textvariable=self.seat_mode_label, state="readonly", width=22,
            values=[SEAT_MODE_LABELS[m] for m in
                    (SEAT_DEFAULT, SEAT_EDIT, SEAT_FILE, SEAT_NONE)])
        self.seat_mode_box.pack(side="left")

        # The file picker, shown only in File mode. (In Edit mode the map itself
        # is the seat selector, so no flat checkbox list is needed.)
        self.seat_extra = tk.Frame(body, bg=COLORS["card"])
        self.seat_extra.grid(row=7, column=0, columnspan=2, sticky="ew")
        self.seat_extra.columnconfigure(0, weight=1)
        self.seat_file_row = tk.Frame(self.seat_extra, bg=COLORS["card"])
        self.seat_file_row.columnconfigure(0, weight=1)
        # One short line; the full explanation is on the combobox tooltip.
        self.seat_file_note = tk.Label(
            self.seat_file_row,
            text="Read from a file in your project; the file is never changed.",
            bg=COLORS["card"], fg=COLORS["faint"], font=self.fonts.small,
            anchor="w", justify="left", wraplength=360)
        self.seat_file_note.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(6, 4))
        self.seat_file_note.bind(
            "<Configure>", lambda e: self.seat_file_note.configure(wraplength=max(e.width - 4, 200)))
        # Detected candidate files in the project, plus a Browse fallback.
        self._candidate_file_map = {}
        self.seat_file_choice = tk.StringVar(self.root, value="")
        self.seat_file_combo = ttk.Combobox(
            self.seat_file_row, textvariable=self.seat_file_choice, state="readonly")
        self.seat_file_combo.grid(row=1, column=0, sticky="ew")
        self.seat_file_combo.bind("<<ComboboxSelected>>", self._on_candidate_file_pick)
        Tooltip(self.seat_file_combo, lambda: (
            "The list is read as-is at launch for this run only: your file is never changed and "
            "its seats are not added to any lab preset. The chosen file is remembered with this "
            "config, like every other field.")).attach(self.seat_file_combo)
        ttk.Button(self.seat_file_row, text="Browse...",
                   command=self.browse_seat_file).grid(row=1, column=1, padx=(8, 0))
        self.seat_file_box = PathBox(self.seat_file_row, self.fonts.body,
                                     self.var["seat_file"], placeholder="No file chosen yet")
        self.seat_file_box.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 2), ipady=1)

        # -- top-down room-layout seat map. The caption is a toggle: the map is
        #    EXPANDED by default (Julian, round 5) and still collapsible; it is
        #    always open in Edit mode where the seats themselves are the selector.
        self._map_expanded = True
        self.map_holder = tk.Frame(body, bg=COLORS["card"])
        self.map_holder.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.map_holder.columnconfigure(0, weight=1)
        self.map_caption = tk.Label(self.map_holder, text="", bg=COLORS["card"],
                                    fg=COLORS["muted"], font=self.fonts.small_bold, anchor="w",
                                    cursor="hand2")
        self.map_caption.grid(row=0, column=0, sticky="w")
        self.map_caption.bind("<Button-1>", self._toggle_map)
        self.map_subcaption = tk.Label(self.map_holder, text="", bg=COLORS["card"],
                                       fg=COLORS["faint"], font=self.fonts.small, anchor="w")
        self.map_subcaption.grid(row=1, column=0, sticky="w", pady=(1, 5))
        self.seat_map = SeatMapCanvas(self.map_holder, self.fonts)
        self.seat_map.grid(row=2, column=0, sticky="ew")

        # -- settings.py block. Nothing shows here when the block is fine (round
        #    6): only when it is MISSING or broken does a short problem line plus
        #    a prominent "Add block to settings.py" fix appear. Viewing the block
        #    when it is fine lives in the gear menu, not on the main page.
        self.block_status = StatusLine(body, self.fonts.small)
        self.block_status.grid(row=9, column=0, columnspan=2, sticky="ew", pady=(10, 2))
        self.block_actions = tk.Frame(body, bg=COLORS["card"])
        self.block_actions.grid(row=10, column=0, columnspan=2, sticky="w")
        self.block_button = tk.Button(
            self.block_actions, text="Add block to settings.py", command=self.add_block,
            font=self.fonts.small_bold, bg=COLORS["accent"], fg="#ffffff",
            activebackground=COLORS["accent_dark"], activeforeground="#ffffff",
            relief="flat", padx=12, pady=5, cursor="hand2")
        self.block_button.grid(row=0, column=0, sticky="w")
        body.bind("<Configure>", self._resize_lab_labels)

    def _rebuild_lab_tiles(self, ncols=2):
        """(Re)build one tile per DISPLAYED lab preset. Called at start-up and
        after the Lab Settings page changes which labs are shown. If exactly one
        lab is displayed it becomes the forced default so a researcher on that
        machine cannot pick the wrong lab."""
        frame = self.lab_tiles_frame
        for parts in self.lab_tiles.values():
            parts["frame"].destroy()
        self.lab_tiles = {}
        if getattr(self, "_single_lab_frame", None) is not None:
            self._single_lab_frame.destroy()
            self._single_lab_frame = None
        for c in range(16):
            frame.columnconfigure(c, weight=0, uniform="")
        presets = core.selectable_lab_presets(self.lab_presets)
        single = (len(presets) == 1)
        if single:
            # One lab on this machine: a plain line, not a big non-clickable tile.
            frame.columnconfigure(0, weight=1)
            self._single_lab_frame = self._make_single_lab_line(frame, presets[0])
        else:
            for idx, preset in enumerate(presets):
                col = idx % ncols
                frame.columnconfigure(col, weight=1, uniform="tile")
                self.lab_tiles[preset["id"]] = self._make_lab_tile(frame, preset, idx, ncols)
        # Force the single-lab default, else keep the current selection if it is
        # still selectable, else fall to the first.
        if getattr(self, "var", None) is not None:
            forced = core.default_selected_lab(self.lab_presets, current=self.var["lab"].get())
            if forced and self.var["lab"].get() != forced:
                self.var["lab"].set(forced)

    def _make_single_lab_line(self, parent, preset):
        # The "Lab" row-label prefix is hidden in the single-lab case (the "3.
        # Lab" header already says Lab), so this line shows only the lab name,
        # IP and seat count, in normal text colour.
        line = tk.Frame(parent, bg=COLORS["card"])
        line.grid(row=0, column=0, sticky="w")
        tk.Label(line, text="%s (%s) · %d seats"
                 % (preset["name"], preset["ip"], len(preset["seats"])),
                 bg=COLORS["card"], fg=COLORS["text"], font=self.fonts.small_bold).pack(side="left")
        return line

    def _make_lab_tile(self, parent, preset, idx, ncols=2):
        lab_value, text = preset["id"], preset["name"]
        row, col = idx // ncols, idx % ncols
        tile = tk.Frame(parent, bg=COLORS["card"], highlightthickness=1,
                        highlightbackground=COLORS["card_line"],
                        highlightcolor=COLORS["card_line"], cursor="hand2")
        tile.grid(row=row, column=col, sticky="ew",
                  padx=((0, 5) if col == 0 else (5, 0)),
                  pady=((0, 0) if row == 0 else (6, 0)))
        inner = tk.Frame(tile, bg=COLORS["card"])
        inner.pack(fill="both", expand=True, padx=10, pady=9)
        label = tk.Label(inner, text=text, bg=COLORS["card"], fg=COLORS["text"],
                         font=self.fonts.bold, anchor="center")
        label.pack()
        # IP + seat count as a small muted second line on the tile itself.
        sub = tk.Label(inner, text="%s · %d seats" % (preset["ip"], len(preset["seats"])),
                       bg=COLORS["card"], fg=COLORS["muted"], font=self.fonts.small,
                       anchor="center")
        sub.pack()
        for widget in (tile, inner, label, sub):
            widget.bind("<Button-1>", lambda _e, v=lab_value: self.var["lab"].set(v))
        return {"frame": tile, "inner": inner, "label": label, "sub": sub}

    def _make_host_button(self, parent, value, text):
        """One of the two host buttons (localhost / Other host…): a small bordered
        button in the lab-tile style, so "selected" looks the same for a lab and
        for a host. Returns its parts for _style_host_buttons."""
        button = tk.Frame(parent, bg=COLORS["card"], highlightthickness=1,
                          highlightbackground=COLORS["card_line"],
                          highlightcolor=COLORS["card_line"], cursor="hand2")
        button.pack(side="left", padx=((0, 6) if value == LAB_LOCAL else (0, 0)))
        label = tk.Label(button, text=text, bg=COLORS["card"], fg=COLORS["text"],
                         font=self.fonts.small_bold, padx=12, pady=4, cursor="hand2")
        label.pack()
        for widget in (button, label):
            widget.bind("<Button-1>", lambda _e, v=value: self.var["lab"].set(v))
        return {"frame": button, "label": label}

    def _style_host_buttons(self):
        """Paint the host buttons: accent when chosen, plain otherwise."""
        lab = self.var["lab"].get()
        for value, parts in getattr(self, "host_buttons", {}).items():
            if lab == value:
                bg, fg, border = COLORS["accent_soft"], COLORS["accent"], COLORS["accent"]
            else:
                bg, fg, border = COLORS["card"], COLORS["text"], COLORS["card_line"]
            parts["frame"].configure(bg=bg, highlightbackground=border, highlightcolor=border)
            parts["label"].configure(bg=bg, fg=fg)

    def _style_tiles(self):
        """Paint each lab tile: accent when chosen, muted while a custom host is."""
        lab = self.var["lab"].get()
        faded = (lab in (LAB_CUSTOM, LAB_LOCAL))
        for value, parts in self.lab_tiles.items():
            if lab == value and not faded:
                bg, fg, subfg, border = (COLORS["accent_soft"], COLORS["accent"],
                                         COLORS["accent"], COLORS["accent"])
            elif faded:
                bg, fg, subfg, border = (COLORS["card"], COLORS["faint"],
                                         COLORS["faint"], COLORS["card_line"])
            else:
                bg, fg, subfg, border = (COLORS["card"], COLORS["text"],
                                         COLORS["muted"], COLORS["card_line"])
            parts["frame"].configure(bg=bg, highlightbackground=border, highlightcolor=border)
            parts["inner"].configure(bg=bg)
            parts["label"].configure(bg=bg, fg=fg)
            parts["sub"].configure(bg=bg, fg=subfg)

    def _toggle_map(self, _event=None):
        self._map_expanded = not getattr(self, "_map_expanded", False)
        self._apply_lab_view()

    def _toggle_custom_more(self):
        self._custom_more_shown = not getattr(self, "_custom_more_shown", False)
        if self._custom_more_shown:
            self.custom_more.grid()
            self.custom_more_link.configure(text="▾  Port and page")
        else:
            self.custom_more.grid_remove()
            self.custom_more_link.configure(text="▸  Port and page")

    def _apply_lab_view(self):
        """Show the parts of the Lab card that fit the chosen lab and seat mode."""
        lab = self.var["lab"].get()
        custom = (lab == LAB_CUSTOM)
        local = (lab == LAB_LOCAL)
        pseudo = custom or local
        mode = self.var["seat_mode"].get()
        self._style_tiles()

        # The two host buttons are always there; "← Back to a lab" joins them
        # only while a pseudo-host (localhost / other host) is the choice.
        self._style_host_buttons()
        if pseudo:
            self.back_to_lab_link.pack(side="left", padx=(12, 0))
        else:
            self.back_to_lab_link.pack_forget()

        # Host/Port/Page/Room + OPENS appear only for a custom host (its host is
        # typed). The Local host is fixed to localhost, so it shows the Local
        # note and the normal Room row instead of the custom host fields.
        if custom:
            self.custom_fields.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(6, 0))
            self.room_row.grid_remove()
            self.local_note.grid_remove()
        elif local:
            self.custom_fields.grid_remove()
            self.local_note.grid()
            self.room_row.grid()
        else:
            self.custom_fields.grid_remove()
            self.local_note.grid_remove()
            self.room_row.grid()

        # The room-layout map: collapsible in Lab-default mode (collapsed by
        # default), auto-opened in Edit mode where the seats are the selector.
        if not pseudo and mode in (SEAT_DEFAULT, SEAT_EDIT):
            interactive = (mode == SEAT_EDIT)
            expanded = interactive or self._map_expanded
            total = len(self._lab_default_seats(self.form_values()))
            if interactive:
                chosen = total - len(self.seat_excluded & set(self._lab_default_seats(self.form_values())))
                self.map_caption.configure(text="ROOM LAYOUT (click seats to include/exclude)")
                self.map_subcaption.configure(text="%d of %d selected" % (chosen, total))
            else:
                arrow = "▾" if expanded else "▸"   # ▾ / ▸
                self.map_caption.configure(text="%s  Room layout · %d seats" % (arrow, total))
                # Round 7: no "Front of the room..." caption above the map.
                self.map_subcaption.configure(text="")
            # The subcaption only carries the interactive seat count now; in Lab-
            # default mode there is nothing to say, so it stays hidden (no gap).
            if expanded:
                self.seat_map.show(self._lab_seatmap_data(lab), excluded=self.seat_excluded,
                                   interactive=interactive, on_toggle=self._toggle_seat)
                self.seat_map.grid()
                if interactive:
                    self.map_subcaption.grid()
                else:
                    self.map_subcaption.grid_remove()
            else:
                self.seat_map.show(None)
                self.seat_map.grid_remove()
                self.map_subcaption.grid_remove()
            self.map_holder.grid()
        else:
            self.seat_map.show(None)
            self.map_holder.grid_remove()

        # settings.py block status + action (or hidden when no seat list).
        self._apply_block_actions()

    def _apply_block_actions(self):
        """Show the block status + fix ONLY when there is a problem (round 6).

        When the block is fine, or there is no settings.py to check yet, or seats
        are off, the main page shows nothing about the block. A missing or broken
        block shows a short problem line plus the prominent Add-block fix.
        """
        if not hasattr(self, "block_actions"):
            return
        state = getattr(self, "block_state", None)
        readable = bool(state and state.get("readable"))
        problem = readable and not (state.get("complete") and not state.get("rooms_after"))
        show = problem and self.var["seat_mode"].get() != SEAT_NONE
        if show:
            self.block_status.grid()
            self.block_actions.grid()
            self.block_button.grid()
        else:
            self.block_status.grid_remove()
            self.block_actions.grid_remove()

    def _resize_lab_labels(self, event):
        self.block_status.set_wraplength(max(event.width - 28, 160))
        # The localhost note wraps to the card too (it used to run off the edge).
        self.local_note.configure(wraplength=max(event.width - 8, 160))

    def _toggle_seat(self, seat):
        """Include/exclude one seat (fired by a click on the interactive map)."""
        if seat in self.seat_excluded:
            self.seat_excluded.discard(seat)
        else:
            self.seat_excluded.add(seat)
        self.var["seat_excluded"].set(json.dumps(sorted(self.seat_excluded)))
        self.refresh_previews()

    def browse_seat_file(self):
        start = os.path.dirname(self.var["seat_file"].get()) or \
            self.var["project_path"].get() or os.path.expanduser("~")
        if not os.path.isdir(start):
            start = os.path.expanduser("~")
        chosen = filedialog.askopenfilename(
            parent=self.root, title="Choose a participant label file",
            initialdir=start, filetypes=[("Text file", "*.txt"), ("All files", "*.*")])
        if chosen:
            self.var["seat_file"].set(os.path.normpath(chosen))
            self.log("Participant label file: %s" % chosen, "info")
            self._refresh_candidate_files()
            self.refresh_previews()

    def _on_candidate_file_pick(self, *_args):
        """Set the seat file from a detected candidate, saved in the config's
        seat_file field so a reloaded config restores the same file."""
        display = self.seat_file_choice.get()
        path = self._candidate_file_map.get(display)
        if path:
            self.var["seat_file"].set(os.path.normpath(path))
            self.log("Participant label file: %s" % path, "info")
            self.refresh_previews()

    def _refresh_candidate_files(self):
        """Populate the candidate-file dropdown from the project, and pre-select
        the config's current seat_file when it is one of them."""
        if not hasattr(self, "seat_file_combo"):
            return
        project = self.var["project_path"].get()
        proj = os.path.abspath(project) if project else ""
        self._candidate_file_map = {}
        values = []
        for path in core.find_candidate_label_files(project):
            if proj and os.path.abspath(path).startswith(proj):
                display = os.path.relpath(path, proj)
            else:
                display = os.path.basename(path)
            self._candidate_file_map[display] = path
            values.append(display)
        self.seat_file_combo.configure(values=values,
                                       state=("readonly" if values else "disabled"))
        current = os.path.normpath(self.var["seat_file"].get()) if self.var["seat_file"].get() else ""
        matched = ""
        for display, path in self._candidate_file_map.items():
            if os.path.normpath(path) == current:
                matched = display
                break
        # Exactly one candidate and nothing chosen yet: pick it automatically.
        if not matched and len(values) == 1 and not current:
            only = values[0]
            self.seat_file_choice.set(only)
            self.var["seat_file"].set(os.path.normpath(self._candidate_file_map[only]))
            matched = only
        else:
            self.seat_file_choice.set(matched)
        # Show the found-count on the note so the picker's usefulness is visible.
        n = len(values)
        if n:
            self.seat_file_note.configure(
                text="%d file%s found in your project. Read as-is; the file is never changed."
                     % (n, "" if n == 1 else "s"))
        else:
            self.seat_file_note.configure(
                text="No .txt files found in your project. Use Browse to pick one.")

    def apply_seat_mode(self):
        """Show the file picker for File mode; every other mode uses the map."""
        mode = self.var["seat_mode"].get()
        self.seat_file_row.grid_remove()
        if mode == SEAT_FILE:
            self._refresh_candidate_files()
            self.seat_file_row.grid(row=0, column=0, sticky="ew", pady=(6, 2))
            # More than one candidate: put focus on the picker so it is the next
            # obvious step (a full auto-open of the dropdown is not reliable in Tk).
            if len(self._candidate_file_map) > 1:
                try:
                    self.seat_file_combo.focus_set()
                except tk.TclError:
                    pass

    # -- log and bottom bar ------------------------------------------------

    def _build_log(self, parent):
        frame = tk.Frame(parent, bg=COLORS["window"])
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        head = tk.Frame(frame, bg=COLORS["window"])
        head.grid(row=0, column=0, sticky="ew", pady=(4, 3))
        # The caption itself is the expand/shrink toggle (▸ / ▾): short by default
        # (the log only matters during a launch), it grows on launch and on click.
        self._log_height_normal = 3
        self._log_expanded = False
        self.log_caption = tk.Label(head, text="ACTIVITY LOG  ▸", bg=COLORS["window"],
                                    fg=COLORS["muted"], font=self.fonts.small_bold,
                                    anchor="w", cursor="hand2")
        self.log_caption.pack(side="left", pady=(2, 0))
        self.log_caption.bind("<Button-1>", lambda _e: self.toggle_log_height())
        # Copy is a small muted text control aligned on the caption's own row, so
        # it reads as part of the ACTIVITY LOG header rather than a floating
        # button (round 7). Clear lives in the log's right-click menu.
        self.log_copy = tk.Label(head, text="Copy", bg=COLORS["window"],
                                 fg=COLORS["muted"], font=self.fonts.small_bold,
                                 cursor="hand2")
        self.log_copy.pack(side="right", padx=(0, 6), pady=(2, 0))
        self.log_copy.bind("<Button-1>", lambda _e: self.copy_log())
        self.log_copy.bind("<Enter>", lambda _e: self.log_copy.configure(fg=COLORS["accent"]))
        self.log_copy.bind("<Leave>", lambda _e: self.log_copy.configure(fg=COLORS["muted"]))

        holder = tk.Frame(frame, bg=COLORS["log_bg"], highlightthickness=1,
                          highlightbackground=COLORS["card_line"])
        holder.grid(row=1, column=0, sticky="nsew")
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)

        self.log_text = tk.Text(
            holder, bg=COLORS["log_bg"], fg=COLORS["log_fg"], font=self.fonts.mono,
            wrap="word", bd=0, highlightthickness=0, relief="flat", padx=10, pady=8,
            insertbackground=COLORS["log_fg"], height=self._log_height_normal, state="disabled",
            selectbackground="#3a4658",
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)

        self.log_text.tag_configure("time", foreground="#6f7d90")
        self.log_text.tag_configure("info", foreground=COLORS["log_fg"])
        self.log_text.tag_configure("muted", foreground="#8e9aab")
        self.log_text.tag_configure("cmd", foreground="#7fd1ff")
        self.log_text.tag_configure("ok", foreground="#7ee0a8")
        self.log_text.tag_configure("warn", foreground="#f2c66b")
        self.log_text.tag_configure("err", foreground="#ff9a90")
        self.log_text.tag_configure("out", foreground="#b6c0cf", lmargin1=22, lmargin2=22)
        # Clear (and Copy) live in a right-click menu on the log itself.
        self.log_text.bind("<Button-3>", self._log_context_menu)
        self.log_text.bind("<Button-2>", self._log_context_menu)   # macOS
        return frame

    def _build_bottom(self, parent):
        bar = tk.Frame(parent, bg=COLORS["window"])
        bar.grid(row=3, column=0, sticky="ew", padx=PAD + 4, pady=(PAD - 2, PAD - 1))
        bar.columnconfigure(0, weight=1)

        self.inline_status = StatusLine(bar, self.fonts.small)
        self.inline_status.configure(bg=COLORS["window"])
        self.inline_status.dot.configure(bg=COLORS["window"])
        self.inline_status.label.configure(bg=COLORS["window"], wraplength=520)
        self.inline_status.grid(row=0, column=0, sticky="ew", padx=(0, 12))

        # "Save one-click shortcut" writes a standalone .bat for the current
        # config; double-clicking it later launches with no UI.  It sits next to
        # Launch (the old gear-menu export is retired in favour of this button).
        self.shortcut_button = tk.Button(
            bar, text="Save one-click shortcut", command=self.save_shortcut,
            font=self.fonts.body, bg=COLORS["window"], fg=COLORS["accent"],
            activebackground=COLORS["window"], activeforeground=COLORS["accent_dark"],
            relief="flat", bd=1, padx=16, pady=8, cursor="hand2",
            highlightthickness=1, highlightbackground=COLORS["accent"],
        )
        self.shortcut_button.grid(row=0, column=1, sticky="e", padx=(0, 10))

        self.launch_button = tk.Button(
            bar, text="Launch", command=self.launch, font=self.fonts.launch,
            bg=COLORS["accent"], fg="#ffffff", activebackground=COLORS["accent_dark"],
            activeforeground="#ffffff", relief="flat", bd=0, padx=34, pady=8,
            cursor="hand2", highlightthickness=0,
        )
        self.launch_button.grid(row=0, column=2, sticky="e")

    def log(self, message, level="info", prefix=True):
        if threading.current_thread() is not threading.main_thread():
            # If the window has already gone, there is nowhere to log to and
            # nothing useful to do about it.
            try:
                self.root.after(0, lambda: self.log(message, level, prefix))
            except (tk.TclError, RuntimeError):
                pass
            return
        self.log_text.configure(state="normal")
        if prefix:
            self.log_text.insert("end", _dt.datetime.now().strftime("[%H:%M:%S] "), "time")
        self.log_text.insert("end", message + "\n", level)
        self.log_text.yview_moveto(1.0)
        self.log_text.configure(state="disabled")

    def toggle_log_height(self):
        """Double the activity-log panel's height, or shrink it back.

        The log frame sits in a non-stretching row, so its height follows the
        Text widget's requested line count, changing that is all it takes.
        """
        self._log_expanded = not self._log_expanded
        if self._log_expanded:
            self.log_text.configure(height=self._log_height_normal * 2)
            self.log_caption.configure(text="ACTIVITY LOG  ▾")
        else:
            self.log_text.configure(height=self._log_height_normal)
            self.log_caption.configure(text="ACTIVITY LOG  ▸")

    def _log_context_menu(self, event):
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Copy log", command=self.copy_log)
        menu.add_command(label="Clear log", command=self.clear_log)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def copy_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log_text.get("1.0", "end-1c"))
        self.log("Log copied to the clipboard.", "muted")

    # -- form <-> config ---------------------------------------------------

    # -- preset-aware wrappers (route host/seat resolution through core so a
    #    lab the researcher ADDED drives the host, seats and URL) -------------

    def _resolve_seats(self, cfg):
        return core.resolve_seats(cfg, self.lab_presets)

    def _lab_default_seats(self, cfg):
        return core.lab_default_seats(cfg, self.lab_presets)

    def _build_url(self, cfg):
        return core.build_url(cfg, self.lab_presets)

    def _prepare_label_file(self, cfg, config_name):
        return core.prepare_label_file(cfg, config_name, self.lab_presets)

    def _lab_seatmap_data(self, lab):
        """The seat-map geometry for a lab id.

        A lab whose lab_info.json entry has a map (a maps/<name>.json reference
        or an inline object) is drawn from that map, with its own seat labels
        filled in. A lab with only a seat list is drawn as a plain grid.
        """
        preset = core.find_lab_preset(lab, self.lab_presets)
        if preset is None:
            return None
        seats = preset.get("seats", [])
        map_obj = preset.get("map")
        if not map_obj:
            # A presets.json written before maps existed has no map on the stored
            # preset; fall back to the map the lab defines in lab_info.json.
            info_preset = core.find_lab_preset(lab, core.default_lab_presets())
            if info_preset is not None:
                map_obj = info_preset.get("map")
        data = core.build_seatmap_from_map(map_obj, seats)
        if data is not None:
            return data
        cols = int(preset.get("cols", 0))   # optional room-shape hint
        return _grid_seatmap(seats, cols) if cols > 0 else _grid_seatmap(seats)

    def form_values(self):
        values = {}
        for key in FIELD_KEYS:
            try:
                raw = self.var[key].get()
            except tk.TclError:
                raw = DEFAULT_CONFIG[key]
            if isinstance(DEFAULT_CONFIG[key], list):
                try:
                    raw = json.loads(raw) if isinstance(raw, str) else raw
                except ValueError:
                    raw = []
            values[key] = raw
        return normalize_config(values)

    def load_fields(self, cfg, index):
        self.loading = True
        try:
            values = normalize_config(cfg)
            for key in FIELD_KEYS:
                value = values[key]
                self.var[key].set(json.dumps(value) if isinstance(value, list) else value)
            # Honor the single-lab forced default and drop a stale lab id that is
            # no longer displayed, so an opened config never selects a hidden lab.
            forced = core.default_selected_lab(self.lab_presets, current=values["lab"])
            if forced and forced != values["lab"] and values["lab"] != LAB_CUSTOM:
                self.var["lab"].set(forced)
            self.seat_excluded = set(values["seat_excluded"])
            self.db_mode_label.set(DB_MODE_LABELS[values["db_mode"]])
            self.seat_mode_label.set(SEAT_MODE_LABELS[values["seat_mode"]])
            self.show_db_password.set(False)
            self.show_admin_password.set(False)
            self._map_expanded = True    # room-layout map starts EXPANDED (round 5)
            for entry, _flag in self._password_entries:
                entry.configure(show=MASK_CHAR)
        finally:
            self.loading = False
        self.selected_index = index
        self.dirty = False
        self.apply_db_mode()
        self.apply_seat_mode()
        self._apply_lab_view()
        self._apply_admin_visibility()   # collapse creds, or open if non-default
        self.refresh_previews()
        self.refresh_dirty()
        self.refresh_sidebar()

    def select_preset(self, index, log_it=True):
        if not (0 <= index < len(self.presets)):
            return
        preset = self.presets[index]
        self.load_fields(preset, index)
        self.inline_status.set("muted", "")
        if log_it:
            self.log('Loaded config "%s".' % preset.get("name", ""), "info")

    def _on_field_change(self, *_args):
        if self.loading:
            return
        self.refresh_previews()
        self.refresh_dirty()

    def _open_db_menu(self, event=None):
        """The pen icon opens the database picker: the oTree default (SQLite),
        the lab shared Postgres, and EVERY custom database in the global registry
        (each with its creator researcher in grey), plus "Add new database…".
        The list is core.list_databases so both launchers show the same picker."""
        DatabasePickerDialog(self.root, self.fonts, self)

    def current_database_id(self):
        """The registry id of the database the on-screen config currently uses,
        for the picker to tick. Matches the built-ins by mode and a custom by its
        connection (name + host + user), or "" when nothing matches."""
        mode = self.var["db_mode"].get()
        if mode == DB_MODE_NONE:
            return core.DB_BUILTIN_SQLITE
        if mode == DB_MODE_LAB:
            return core.DB_BUILTIN_LAB
        name = self.var["db_name"].get().strip()
        host = self.var["db_host"].get().strip()
        user = self.var["db_user"].get().strip()
        for entry in core.known_databases_from_store(self.store_extra):
            if (entry["db_name"] == name and entry["db_host"] == host
                    and entry["db_user"] == user):
                return entry["id"]
        return ""

    def choose_database(self, entry):
        """Apply a picked database entry to the on-screen config (built-in or
        custom). One path for all three: core.database_config_fields decides the
        db_mode and, for a custom database, fills the connection fields."""
        fields = core.database_config_fields(entry)
        for key, value in fields.items():
            if key in self.var:
                self.var[key].set(value)
        self.db_mode_label.set(DB_MODE_LABELS[fields["db_mode"]])
        self.apply_db_mode()
        self.refresh_previews()
        self.log("Database set to %s." % entry.get("title", "the chosen database"), "info")

    def _choose_db_mode(self, mode):
        """Switch the database to ``mode`` from the pen menu and refresh the card.

        For the lab shared database the known lab credentials are re-applied; the
        connection fields are revealed (and made editable) only for a custom
        database. ``db_mode_label`` is kept as a plain mirror for display."""
        self.var["db_mode"].set(mode)
        if mode == DB_MODE_LAB and not self.loading:
            for key, value in core.LAB_DB.items():
                self.var[key].set(value)
        self.db_mode_label.set(DB_MODE_LABELS[mode])
        self.apply_db_mode()
        self.refresh_previews()

    def _on_seat_mode_change(self, *_args):
        mode = SEAT_MODE_BY_LABEL.get(self.seat_mode_label.get(), SEAT_DEFAULT)
        if self.var["seat_mode"].get() != mode:
            self.var["seat_mode"].set(mode)
        self.apply_seat_mode()
        self._apply_lab_view()
        self.refresh_previews()

    def _on_lab_change(self, *_args):
        # A normal lab pins the room to the shortcut's room and comes with a seat
        # list; a custom host has no known seat list, so it drops to an open room.
        # Exclusions the user made for tonight are remembered, resolve_seats only
        # applies the ones that exist in the new lab's list.
        if self.loading:
            return
        lab = self.var["lab"].get()
        # A pseudo-host (typed custom host, or Local/localhost) has no known seat
        # list, so it drops to an open room; a real lab pins the shortcut room and
        # comes with a seat list.
        if lab not in (LAB_CUSTOM, LAB_LOCAL):
            if self.var["room_name"].get().strip() != DEFAULT_ROOM_NAME:
                self.var["room_name"].set(DEFAULT_ROOM_NAME)
            if self.var["seat_mode"].get() == SEAT_NONE:
                self.seat_mode_label.set(SEAT_MODE_LABELS[SEAT_DEFAULT])
        elif lab == LAB_CUSTOM:
            self.seat_mode_label.set(SEAT_MODE_LABELS[SEAT_NONE])
        else:  # LAB_LOCAL: keep the room the study default, but no seat board
            if self.var["room_name"].get().strip() != DEFAULT_ROOM_NAME:
                self.var["room_name"].set(DEFAULT_ROOM_NAME)
            self.seat_mode_label.set(SEAT_MODE_LABELS[SEAT_NONE])
        self._apply_lab_view()

    def apply_db_mode(self):
        mode = self.var["db_mode"].get()
        state = {"lab": "readonly", "custom": "normal", "none": "disabled"}[mode]
        for entry in self.db_entries.values():
            entry.configure(state=state)
        if mode != DB_MODE_CUSTOM:
            self.set_password_visible("db", False)
        # The connection fields (host/user/password/name/port + DATABASE_URL
        # preview) appear, editable, ONLY for a custom database; lab and oTree-
        # default need nothing typed, so the card stays a single line for them.
        if mode == DB_MODE_CUSTOM:
            self.db_section.grid()
        else:
            self.db_section.grid_remove()
        self._refresh_db_summary()

    def _db_summary_text(self):
        # The reset state is shown by the always-visible "Reset before start"
        # tickbox on the same line, so it is NOT repeated in this summary text.
        mode = self.var["db_mode"].get()
        if mode == DB_MODE_LAB:
            return "Lab shared database (Postgres)"
        if mode == DB_MODE_NONE:
            return "oTree default (SQLite)"
        name = self.var["db_name"].get().strip() or "custom database"
        host = self.var["db_host"].get().strip() or "localhost"
        return "%s on %s (Postgres)" % (name, host)

    def _refresh_db_summary(self):
        if hasattr(self, "db_summary"):
            self.db_summary.set(self._db_summary_text())

    def _lab_label(self, lab):
        preset = core.find_lab_preset(lab, self.lab_presets)
        if lab == LAB_CUSTOM:
            return "Custom host"
        if preset is not None:
            return preset.get("name") or lab
        return lab or "Custom host"

    def _refresh_run_summary(self, cfg=None):
        """No-op: Julian removed the one-line run summary (round 6). The bottom
        status line (inline_status) is kept for launch progress and errors only,
        so it stays blank during normal editing rather than restating the config.
        """
        return

    def set_password_visible(self, which, visible):
        """Show or hide one of the two password fields.

        A stored password is always masked until somebody asks for it here.
        """
        entry = self.db_entries["db_password"] if which == "db" else self.admin_password_entry
        flag = self.show_db_password if which == "db" else self.show_admin_password
        flag.set(bool(visible))
        entry.configure(show="" if visible else MASK_CHAR)

    def refresh_previews(self):
        cfg = self.form_values()
        if cfg["db_mode"] == DB_MODE_NONE:
            self.db_url_preview.set("not set - oTree falls back to its own default database")
            self.db_url_label.configure(fg=COLORS["muted"])
        else:
            self.db_url_preview.set(mask_database_url(build_database_url(cfg)))
            self.db_url_label.configure(fg=COLORS["text"])
        self.url_preview.set(self._build_url(cfg))
        self._refresh_db_summary()
        self._refresh_run_summary(cfg)

        # Project validation: a one-line summary, plus the app packages as a
        # title-cased bullet list when the folder really is an oTree project.
        level, message = validate_project(cfg["project_path"])
        if level == "ok":
            apps = find_app_packages(cfg["project_path"])
            self.project_status.set("ok",
                "Looks like an oTree project: settings.py and %d app package%s"
                % (len(apps), "" if len(apps) == 1 else "s"))
            self._set_app_bullets(apps)
        else:
            self.project_status.set(level, message)
            self._set_app_bullets([])
        self.refresh_seats(cfg)

    def refresh_seats(self, cfg=None):
        cfg = cfg or self.form_values()
        mode = cfg["seat_mode"]
        if mode == SEAT_FILE:
            labels = []
            path = cfg["seat_file"].strip()
            if path and os.path.isfile(path):
                try:
                    labels = read_seat_file(path)
                except OSError:
                    labels = []
        else:
            labels = self._resolve_seats(cfg)
        self.seat_count.set(self._seat_count_text(cfg, mode, labels))
        self.seat_list_preview.set(seat_preview(labels))
        # Keep the interactive map's dimming and its "N of M selected" caption in
        # step with the current exclusions.
        self._refresh_map_selection(cfg, mode)
        self.refresh_block_status(cfg)

    def _seat_count_text(self, cfg, mode, labels):
        """The short seat tally shown next to the participant-list dropdown."""
        if mode == SEAT_NONE:
            return ""
        if mode == SEAT_FILE:
            return seat_summary(cfg, None)
        n = len(labels)
        if n == 0:
            return "No seats"
        base = "%d seat%s" % (n, "" if n == 1 else "s")
        nums = [int(x) for x in labels if x.isdigit()]
        if len(nums) == n and sorted(nums) == list(range(min(nums), max(nums) + 1)):
            base += " (%d–%d)" % (min(nums), max(nums))
        return base

    def _refresh_map_selection(self, cfg, mode):
        """Redraw the map's exclusions and update its caption without a rebuild."""
        if cfg["lab"] == LAB_CUSTOM or mode not in (SEAT_DEFAULT, SEAT_EDIT):
            return
        interactive = (mode == SEAT_EDIT)
        self.seat_map.show(self._lab_seatmap_data(cfg["lab"]), excluded=self.seat_excluded,
                           interactive=interactive, on_toggle=self._toggle_seat)
        if interactive:
            total = len(self._lab_default_seats(cfg))
            chosen = len(self._resolve_seats(cfg))
            self.map_subcaption.configure(
                text="Click a seat to include or exclude it · %d of %d selected"
                % (chosen, total))

    def refresh_block_status(self, cfg=None):
        cfg = cfg or self.form_values()
        state = inspect_settings(cfg["project_path"])
        self.block_state = state
        # Only the problem states carry a message; when the block is fine (or
        # there is nothing to check) _apply_block_actions hides the line entirely,
        # so no green all-good reassurance clutters the page (round 6).
        if state["readable"] and not state["has_block"]:
            self.block_status.set("error", state["message"])
        elif state["readable"] and (state["rooms_after"] or not state["complete"]):
            self.block_status.set("warn", state["message"])
        self._apply_block_actions()

    def refresh_dirty(self):
        if self.selected_index is None:
            self.dirty = False
            self.subtitle.configure(text="Config: unsaved settings")
            self._apply_save_affordance()
            self.refresh_sidebar()
            return
        preset = self.presets[self.selected_index]
        was = self.dirty
        # For the built-in default, the project folder is an input to the run,
        # not an edit of the config, so browsing to a project does not mark it
        # modified (no "modified" badge on a normal run).
        ignore = ("project_path",) if is_builtin(preset) else ()
        self.dirty = configs_differ(self.form_values(), preset, ignore=ignore)
        shown = display_name(preset)   # lab suffix on the built-in default only
        self.subtitle.configure(text="Config: %s" % shown)   # no "(unsaved changes)"
        self._apply_save_affordance()
        if was != self.dirty:
            self.refresh_sidebar()

    def _needs_save(self):
        """True when the on-screen setup is not a saved, named config: a brand-new
        blank setup or the built-in default with a project folder chosen, or a
        config that has been changed (dirty). Drives the gentle Save-as-new
        affordance and the save reminder at launch."""
        try:
            has_folder = bool(self.var["project_path"].get().strip())
        except tk.TclError:
            has_folder = False
        if self.selected_index is None or not (0 <= self.selected_index < len(self.presets)):
            return has_folder
        preset = self.presets[self.selected_index]
        if is_builtin(preset):
            return has_folder or self.dirty
        return self.dirty

    def _apply_save_affordance(self):
        """Show the gentle, positive 'Save as new config...' strip when the setup
        is worth saving, and hide it otherwise. Positive framing only, never an
        alarm."""
        if not hasattr(self, "banner"):
            return
        if self._needs_save():
            self.banner_label.configure(
                text="Save this setup as a named config so you can reuse it.")
            self.banner.grid(row=1, column=0, sticky="ew", padx=PAD + 4, pady=(2, 3))
        else:
            self.banner.grid_remove()

    # -- sidebar actions ---------------------------------------------------

    def browse_project(self, parent=None, offer_get_ready=True):
        """The project folder picker behind the main "Browse..." button.

        The Before-you-launch screen's inline [Choose folder…] fix reuses this
        SAME picker (``parent`` = that modal, so the native dialog stacks above
        it; ``offer_get_ready=False`` because the screen already lists the
        missing-block warning with its own one-click fix, and a second modal on
        top of it would only get in the way). Returns the chosen path, or ""
        when the picker was cancelled."""
        start = self.var["project_path"].get() or os.path.expanduser("~")
        if not os.path.isdir(start):
            start = os.path.expanduser("~")
        chosen = filedialog.askdirectory(
            parent=parent or self.root, title="Choose the oTree project folder",
            initialdir=start)
        if not chosen:
            return ""
        path = os.path.normpath(chosen)
        self.var["project_path"].set(path)
        self._remember_project(path)
        level, message = validate_project(chosen)
        self.log("Project folder: %s" % chosen, "info")
        self.log(message, {"ok": "ok", "warn": "warn", "error": "err"}[level])
        # Right after a fresh selection, offer the one-click lab setup if the
        # project is not ready. Shown at most once, here, per selection.
        if offer_get_ready:
            self._maybe_offer_get_ready(path)
        return path

    def _prefill_last_project(self):
        """Prefill the last project folder used on this machine into the built-in
        default, so the common default run opens with a project already set."""
        if self.selected_index is None:
            return
        preset = self.presets[self.selected_index]
        if not is_builtin(preset) or self.var["project_path"].get().strip():
            return
        last = str(self.store_extra.get("last_project", "")).strip()
        if last and os.path.isdir(last):
            self.var["project_path"].set(os.path.normpath(last))

    def _remember_project(self, path):
        """Remember the last project folder per machine (in store_extra)."""
        path = os.path.normpath(path)
        if self.store_extra.get("last_project") != path:
            self.store_extra["last_project"] = path
            try:
                save_store(self.presets, self.store_extra, self.store_path)
            except OSError:
                pass

    def save_as_new(self, on_success=None):
        """Save the on-screen settings as a new config. ``on_success`` (used by
        the save-and-launch flow) is called only after a successful save."""
        suggestion = ""
        # Prefill the Researcher field with the last author used on this machine
        # (remembered across sessions in store_extra), else the OS username.
        author = str(self.store_extra.get("last_author", "")).strip() or default_author()
        if self.selected_index is not None:
            suggestion = self.presets[self.selected_index].get("name", "") + " (copy)"
        dialog = NameDialog(self.root, self.fonts, suggestion,
                            lambda name: unique_name(name, self.presets), author=author,
                            researchers=core.list_researchers(self.store_extra, self.presets))
        name = dialog.result
        if not name:
            return
        preset = preset_from_fields(name, self.form_values(), author=dialog.author)
        self.presets.append(preset)
        # Remember the author for next time (JSON-serialisable; unknown keys in
        # store_extra are preserved by save_store). The author also joins the
        # shared researcher roster (the same one the create-database dialog uses).
        if preset.get("author"):
            self.store_extra["last_author"] = preset["author"]
            core.add_researcher(self.store_extra, preset["author"])
        if not self._persist():
            self.presets.remove(preset)
            return
        self.selected_index = self._index_of(preset)
        self.dirty = False
        self.refresh_dirty()
        self.refresh_sidebar()
        self.log('Saved a new config "%s". Existing configs were not changed.' % name, "ok")
        if on_success is not None:
            on_success()

    def delete_selected(self):
        if self.selected_index is None:
            return
        self._delete_preset(self.presets[self.selected_index])

    def _delete_preset(self, preset):
        """Delete one config (from the Delete button or a row's hover x)."""
        name = preset.get("name", "")
        # Built-in / built-in configs are the shared, app-owned defaults:
        # they can never be deleted, so nobody can remove a colleague's baseline.
        if is_builtin(preset):
            messagebox.showinfo(
                "Cannot delete",
                '"%s" is a built-in config and cannot be deleted.' % name,
                parent=self.root)
            return
        if not messagebox.askyesno(
            "Delete config",
            'Delete the config "%s"?\n\nThis cannot be undone. The fields stay on screen, '
            "so you can save them again under another name." % name,
            parent=self.root, icon="warning", default="no",
        ):
            return
        selected_preset = (self.presets[self.selected_index]
                           if self.selected_index is not None else None)
        self.presets = [p for p in self.presets if p is not preset]
        if selected_preset is preset or selected_preset is None:
            self.selected_index = None
        else:
            self.selected_index = self._index_of(selected_preset)
        if not self._persist():
            return
        if selected_preset is preset:
            self.dirty = False
            self.subtitle.configure(text="Config: unsaved settings")
            self.banner.grid_remove()
        self.refresh_sidebar()
        self.log('Deleted the config "%s".' % name, "warn")

    def new_blank(self):
        blank = dict(DEFAULT_CONFIG)
        blank["project_path"] = ""
        # Start a new blank config on the lab's chosen default database (set in
        # Lab Settings > Default database); falls back to the code default.
        default_db = str(self.store_extra.get("default_database", "")).strip()
        entry = core.find_database(self.store_extra, default_db) if default_db else None
        if entry is not None:
            blank.update(core.database_config_fields(entry))
        self.load_fields(blank, None)
        self.inline_status.set("muted", "")
        self.log("New blank config. Choose a project folder, then Save as new.", "info")

    # -- first-launch lab identity ----------------------------------------

    def prompt_lab_identity(self):
        """First-launch, one-time modal: which lab is this computer in?

        Shown only when lab.local is unset. Lists the lab presets (plus a
        "different lab, create a new lab" option) and, on a choice, records it
        in lab.local and narrows the machine to that one lab. It stays changeable
        afterwards in Lab Settings. Closing without choosing leaves the machine
        unset, so the prompt returns on the next launch.
        """
        if read_lab_marker() is not None:
            return
        top = self.identity_top = tk.Toplevel(self.root)
        top.title("Which lab is this computer in?")
        top.configure(bg=COLORS["card"])
        _attach_shade(self.root, top)
        top.transient(self.root)
        top.resizable(False, False)

        body = tk.Frame(top, bg=COLORS["card"])
        body.pack(fill="both", expand=True, padx=20, pady=18)
        tk.Label(body, text="Which lab is this computer in?", bg=COLORS["card"],
                 fg=COLORS["text"], font=self.fonts.bold, anchor="w").pack(fill="x")
        tk.Label(body, text="First launch on this machine: pick the lab it sits in. The "
                            "launcher then shows just that lab.",
                 bg=COLORS["card"], fg=COLORS["muted"], font=self.fonts.small, anchor="w",
                 justify="left", wraplength=420).pack(fill="x", pady=(4, 12))

        choices = tk.Frame(body, bg=COLORS["card"])
        choices.pack(fill="x")
        for preset in self.lab_presets:
            self._identity_choice_button(choices, preset, top)

        ttk.Separator(body, orient="horizontal").pack(fill="x", pady=(10, 8))
        tk.Button(body, text="This is a different lab: create a new lab",
                  command=lambda: self._create_lab_for_identity(top),
                  font=self.fonts.small_bold, bg=COLORS["card"], fg=COLORS["accent"],
                  activebackground=COLORS["card"], activeforeground=COLORS["accent_dark"],
                  relief="flat", cursor="hand2", anchor="w", justify="left").pack(fill="x")

        tk.Label(body, text="Saved to lab.local on this machine and remembered next time. You "
                            "can change it later in Lab Settings.",
                 bg=COLORS["card"], fg=COLORS["faint"], font=self.fonts.small, anchor="w",
                 justify="left", wraplength=420).pack(fill="x", pady=(12, 0))

        top.bind("<Escape>", lambda _e: top.destroy())
        _center_on(self.root, top)
        top.update_idletasks()
        try:
            _grab_modal(top)
        except tk.TclError:
            pass

    def _identity_choice_button(self, parent, preset, dialog):
        """One selectable lab row in the first-run chooser (name, seats, IP)."""
        btn = tk.Button(
            parent,
            text="%s\n%d seats · %s" % (preset["name"], len(preset["seats"]), preset["ip"]),
            command=lambda p=preset: self._set_lab_identity(p["id"], dialog),
            font=self.fonts.body, bg=COLORS["field_off"], fg=COLORS["text"],
            activebackground=COLORS["accent_soft"], relief="flat",
            highlightthickness=1, highlightbackground=COLORS["card_line"],
            cursor="hand2", justify="left", anchor="w", padx=12, pady=8)
        btn.pack(fill="x", pady=(0, 6))
        return btn

    def _create_lab_for_identity(self, dialog):
        """From the first-run chooser: run the same add-a-lab flow as Lab
        Settings, then adopt the new lab as this machine's identity."""
        def on_save(name, ip, seats, cols=0):
            ok, message, new_list, preset = core.add_lab_preset(
                self.lab_presets, name, ip, seats, cols=cols)
            if ok:
                self.store_extra["lab_presets"] = new_list
                self.lab_presets = core.lab_presets_from_store(self.store_extra)
                self._persist_store()
                self._set_lab_identity(preset["id"], dialog)
            return ok, message
        LabPresetEditDialog(dialog, self.fonts, "Create a new lab", None, on_save)

    def set_machine_lab(self, lab_id):
        """Set which lab this computer is, from the Lab Settings UI. Updates the
        lab.local marker and narrows the main selector to that lab."""
        self._set_lab_identity(lab_id, dialog=None)

    def _set_lab_identity(self, lab_id, dialog=None, log=True):
        """Record ``lab_id`` as this machine's lab and narrow the UI to it.

        Writes lab.local (overwriting any previous choice, this is the
        UI-settable path), makes that lab the only displayed one via the existing
        display toggles, points the built-in default at it, persists, and
        repaints. Shared by the first-run chooser and the Lab Settings control.
        """
        lab_id = str(lab_id)
        try:
            set_lab_marker(lab_id)
        except (OSError, ValueError) as error:
            messagebox.showerror(
                "Could not set the lab",
                "lab.local could not be written:\n\n%s" % error, parent=self.root)
            return
        # Make it the ONLY displayed lab (reuse the display-toggle machinery), so
        # the machine shows just its one lab.
        ok, _msg, new_list = core.apply_lab_identity(self.lab_presets, lab_id)
        if ok:
            self.store_extra["lab_presets"] = new_list
            self.lab_presets = core.lab_presets_from_store(self.store_extra)
        # Point the built-in default's lab at this machine's lab.
        apply_lab_marker(self.presets, lab_id)
        if dialog is not None:
            try:
                dialog.destroy()
            except tk.TclError:
                pass
        self._persist_store()
        # Repaint the main selector to the single lab and reflect it in the form.
        self._rebuild_lab_tiles()
        selected = self.presets[self.selected_index] if self.selected_index is not None else None
        self._apply_lab_view()
        if selected is not None and is_builtin(selected) and not self.dirty:
            idx = self._index_of(selected)
            if idx is not None:
                self.select_preset(idx, log_it=False)
        else:
            self.refresh_previews()
        self.refresh_sidebar()
        preset = core.find_lab_preset(lab_id, self.lab_presets)
        label = preset["name"] if preset else lab_id
        if log:
            self.log("This machine is set to %s (saved to lab.local)." % label, "ok")

    def _persist(self):
        try:
            save_store(self.presets, self.store_extra, self.store_path)
            return True
        except OSError as error:
            messagebox.showerror(
                "Could not save", "The config file could not be written:\n\n%s\n\n%s"
                % (self.store_path, error), parent=self.root)
            self.log("Could not write %s: %s" % (self.store_path, error), "err")
            return False

    def show_block(self):
        cfg = self.form_values()
        state = inspect_settings(cfg["project_path"])
        self.block_state = state
        BlockDialog(self.root, self.fonts, state, self.add_block, self.log)
        self.refresh_block_status()

    def add_block(self):
        """Append the block to settings.py, after a timestamped backup."""
        cfg = self.form_values()
        state = inspect_settings(cfg["project_path"])
        if not state["readable"]:
            messagebox.showerror("No settings.py", "Could not read %s" % state["path"],
                                 parent=self.root)
            return False
        if state["has_block"]:
            messagebox.showinfo("Already there",
                                "settings.py already has the oTree lab support block.",
                                parent=self.root)
            return False
        if not messagebox.askyesno(
            "Add the block to settings.py",
            "This changes a file in somebody's oTree project.\n\n%s\n\nA timestamped copy "
            "is saved next to it first. Continue?" % state["path"],
            parent=self.root, icon="warning", default="no",
        ):
            return False
        try:
            backup, path = append_block(cfg["project_path"])
        except OSError as error:
            messagebox.showerror("Could not add the block", str(error), parent=self.root)
            self.log("Could not add the block: %s" % error, "err")
            return False
        self.log("Backed up settings.py to %s" % backup, "ok")
        self.log("Appended the oTree lab support block to %s" % path, "ok")
        self.refresh_block_status()
        return True

    # -- get ready for the lab (proactive one-click setup) -----------------

    def _maybe_offer_get_ready(self, path):
        """After a folder is chosen, offer a one-click lab setup if it is not
        lab-ready. Only when settings.py is present and the lab support block is
        missing, cut off, or overridden by a later ROOMS line; shown at most
        once per selection because this is the only place that calls it."""
        state = inspect_settings(path)
        if not state["readable"]:
            return
        not_ready = (not state["has_block"] or not state["complete"]
                     or state["rooms_after"])
        if not not_ready:
            return
        GetReadyDialog(self.root, self.fonts, self._get_ready_for_lab)

    def _get_ready_for_lab(self):
        """Append the block on behalf of the Get-ready popup.

        Reuses the same append_block (timestamped .bak, one source of truth for
        the block) and the same refuse-to-append-twice guard as the manual
        button. Returns (ok, message) for the popup to show inline."""
        cfg = self.form_values()
        state = inspect_settings(cfg["project_path"])
        if not state["readable"]:
            return False, "Could not read settings.py, so nothing was changed."
        if state["has_block"]:
            return False, ("settings.py already has the lab support block, so nothing was added. "
                           "If a ROOMS line follows the block, move the block to the end.")
        try:
            backup, path = append_block(cfg["project_path"])
        except OSError as error:
            self.log("Could not add the block: %s" % error, "err")
            return False, "Could not update settings.py: %s" % error
        self.log("Backed up settings.py to %s" % backup, "ok")
        self.log("Appended the oTree lab support block to %s" % path, "ok")
        self.refresh_block_status()
        return True, "Done. Now press Launch, pick your lab, and go."

    # -- one-click shortcut ------------------------------------------------

    def save_shortcut(self):
        """Save a LIVE one-click shortcut for the current SAVED config.

        The shortcut calls the launcher headlessly (``otree_lab_launcher.py
        --run "<name>"``) so it always reflects the latest saved settings and
        the DB password is NOT baked into a loose file -- the secret stays in
        presets.json. It therefore requires a saved, unmodified config; if the
        current setup is unsaved or edited, the user is asked to save it first.
        Windows gets a no-console ``.vbs`` (pythonw); macOS a ``.command``."""
        if self.selected_index is None or self.dirty:
            messagebox.showinfo(
                "Save the config first",
                "A one-click shortcut runs a saved config by name, so the "
                "password never has to be written into the shortcut file.\n\n"
                "Save these settings as a named config first (Save as new), then "
                "create the shortcut.",
                parent=self.root)
            self.log("Save these settings as a named config first, then create the "
                     "one-click shortcut.", "warn")
            return
        name = self.presets[self.selected_index].get("name", "config")
        shortcut = core.headless_shortcut(name, os.path.abspath(__file__))
        target = filedialog.asksaveasfilename(
            parent=self.root, title="Save one-click shortcut",
            defaultextension=shortcut["ext"], initialfile=shortcut["filename"],
            filetypes=[("One-click shortcut", "*" + shortcut["ext"]), ("All files", "*.*")])
        if not target:
            return
        try:
            with open(target, "w", encoding="utf-8", newline="") as handle:
                handle.write(shortcut["content"])
        except OSError as error:
            messagebox.showerror("Could not save shortcut", str(error), parent=self.root)
            self.log("Saving the shortcut failed: %s" % error, "err")
            return
        self.log("Saved one-click shortcut: %s" % target, "ok")
        self.log('Double-click it to launch the saved config "%s". The database '
                 "password is not stored in the file." % name, "muted")

    # -- Lab Settings (admin config + lab presets, Feature 4) --------------

    def open_lab_settings(self):
        LabSettingsDialog(self.root, self.fonts, self)

    def _persist_store(self):
        """Save presets + extra (which now carries lab_presets and pg_admin)."""
        try:
            save_store(self.presets, self.store_extra, self.store_path)
        except OSError as error:
            messagebox.showerror("Could not save", str(error), parent=self.root)

    def apply_lab_presets_change(self):
        """Re-read lab presets from the store and repaint the main selector.

        Called by the Lab Settings dialog after any add/edit/delete/toggle so a
        newly shown lab appears (and a hidden one disappears) in the tiles, with
        the single-lab forced default honored."""
        self.lab_presets = core.lab_presets_from_store(self.store_extra)
        self._rebuild_lab_tiles()
        self._apply_lab_view()
        self.refresh_previews()

    # -- Create a new database (Feature 2) ---------------------------------

    def create_database_dialog(self):
        admin = core.pg_admin_from_store(self.store_extra)
        missing = core.pg_admin_ready(admin)
        if missing:
            messagebox.showinfo(
                "Fill in the admin details first",
                "The Postgres admin details are not complete (%s).\n\n"
                "Open Lab Settings (the gear at the top left) and fill in the admin "
                "username, password, host and port before creating a database."
                % ", ".join(missing),
                parent=self.root)
            return
        roster = core.list_researchers(self.store_extra, self.presets)
        suggested_researcher = str(self.store_extra.get("last_author", "")).strip() or default_author()
        CreateDatabaseDialog(self.root, self.fonts, self,
                             suggested_name=self._suggested_db_name(),
                             researchers=roster,
                             suggested_researcher=suggested_researcher)

    def _suggested_db_name(self):
        """A database name prefilled from the project folder name (lower-cased,
        non-identifier characters replaced), or "" when it would be invalid."""
        folder = final_folder_name(self.var["project_path"].get())
        if not folder:
            return ""
        slug = re.sub(r"[^a-z0-9_]+", "_", folder.lower()).strip("_")
        if slug and slug[0].isdigit():
            slug = "db_" + slug
        ok, _msg = core.validate_pg_identifier(slug) if slug else (False, "")
        return slug if ok else ""

    def _run_create_database(self, name, user, password, researcher, on_done):
        """Do the create off the UI thread and hand the result back on it. On a
        confirmed create, ALSO register the database in the global registry with
        its creator researcher (the Postgres user is recorded separately), so it
        appears in every config's picker afterward."""
        admin = core.pg_admin_from_store(self.store_extra)
        result = core.create_database(admin, name, new_user=user, new_password=password)
        if result.get("ok"):
            fields = result.get("fields") or {}
            try:
                entry = core.register_database(
                    self.store_extra, title=name, researcher=researcher,
                    connection=fields, postgres_user=fields.get("db_user", ""))
                self._persist_store()
                result["registered"] = entry
            except Exception as error:   # registration must never lose the DB
                result["register_error"] = str(error)
        self._on_main(lambda: on_done(result))

    def apply_created_database(self, fields):
        """Auto-fill the Custom database config from a confirmed create, and save
        it as the current on-screen config (a Custom setup)."""
        self.db_mode_label.set(DB_MODE_LABELS[DB_MODE_CUSTOM])
        self.var["db_mode"].set(DB_MODE_CUSTOM)
        for key in ("db_name", "db_user", "db_password", "db_host", "db_port"):
            if key in fields:
                self.var[key].set(fields[key])
        self.apply_db_mode()
        self.refresh_previews()
        self.log("Custom database fields filled in from the new database.", "ok")

    def offer_save_after_create(self):
        """After a successful create, offer to keep it as a new config (a private
        database is exactly what a researcher wants to reuse)."""
        if messagebox.askyesno(
            "Save this setup?",
            "The database is created and the Custom database fields are filled in.\n\n"
            "Save these settings as a new config so you can reuse this database next time?",
            parent=self.root):
            self.save_as_new()

    # -- Room picker (Feature 3) -------------------------------------------

    def pick_room(self):
        RoomPickerDialog(self.root, self.fonts, self)

    def enumerate_rooms(self, project_path):
        return core.enumerate_project_rooms(project_path)

    def set_room(self, name):
        name = (name or "").strip() or DEFAULT_ROOM_NAME
        self.var["room_name"].set(name)
        self.log("Room set to %r for this run." % name, "info")

    def _update_room_default_tag(self):
        """Show the muted "(lab default)" tag only for the study room."""
        tag = getattr(self, "room_default_tag", None)
        if tag is None:
            return
        is_default = (self.var["room_name"].get().strip() == DEFAULT_ROOM_NAME)
        try:
            if is_default:
                tag.pack(side="left")
            else:
                tag.pack_forget()
        except tk.TclError:
            pass

    # -- launching ---------------------------------------------------------

    def launch(self):
        if self.running:
            self.log("A launch is already running.", "warn")
            return
        cfg = self.form_values()
        self._show_before_launch(cfg)

    def gather_launch_issues(self, cfg):
        """Everything worth telling the user before a launch, as one ordered list.

        Merges the hard blocks (things that make a launch impossible and have no
        one-click fix), the fail-SOFT preflight warnings (database, port,
        project/room, oTree, psycopg2) and the "no lab support block yet" case
        (a warning with an inline one-click fix). Each entry is a dict:
        ``{level: 'block'|'warn', title, hint, fix, fix_label}`` where ``fix`` is
        either None or a callable returning ``(ok, message)``. Rebuilt on demand
        so the consolidated screen can re-check itself after an inline fix."""
        issues = []
        folder_problem = self._project_folder_problem(cfg)
        for message in self._hard_block_problems(cfg):
            issue = {"level": "block", "title": message, "hint": "",
                     "fix": None, "fix_label": ""}
            if folder_problem and message == folder_problem:
                # Tagged with its field so the softer preflight warning about the
                # same folder is suppressed below (one item per field).
                issue["field"] = core.FIELD_PROJECT_PATH
                # Fixable right here: the same folder picker as the main Browse.
                issue["fix"] = lambda cfg=cfg: self._choose_folder_fix(cfg)
                issue["fix_label"] = "Choose folder…"
            issues.append(issue)
        # Missing lab support block: not a hard block (you can launch without it),
        # and the launcher can add it in place, so it is a one-click-fixable warning.
        if self._project_needs_block(cfg):
            issues.append({
                "level": "warn",
                "title": "This project has no oTree lab support block, so the lab "
                         "room, seat board and lab database won’t take effect "
                         "without it.",
                "hint": "Adds a clearly-marked block to the end of settings.py, "
                        "after a timestamped .bak backup (fully revertible). The "
                        "manual copy/paste is on the info screen.",
                "fix": self._get_ready_for_lab, "fix_label": "Add it for me",
                "info": "block"})
        for failure in core.preflight_failures(core.preflight(cfg, core.load_lab_info())):
            # The room is not in the project's static ROOMS, but the lab support
            # block is present and seats will be written: the block defines that
            # room at launch, so the blocker is resolved. This is what clears the
            # room issue after the "Add <room> room" action appends the block.
            if failure.get("kind") == "room" and self._room_will_be_defined_by_block(cfg):
                continue
            issue = {"level": "warn", "title": failure.get("message", ""),
                     "hint": str(failure.get("detail", "")),
                     "fix": None, "fix_label": ""}
            if failure.get("field"):
                issue["field"] = failure["field"]
            self._attach_inline_fix(issue, failure, cfg)
            issues.append(issue)
        # A hard blocker on a field suppresses the softer warning about that same
        # field: with no project folder, only the must-fix (with Choose folder…)
        # shows, not also the preflight "Project folder not found" warning.
        return core.suppress_shadowed_warnings(issues)

    def _choose_folder_fix(self, cfg):
        """Inline fix for the project-folder must-fix: open the SAME folder
        picker the main "Browse..." uses, above the pre-launch screen. On a pick
        the form AND the cfg dict the launch will use are updated, and the screen
        re-checks itself. A cancelled picker changes nothing (returns None, so
        the screen shows no note)."""
        dialog = getattr(self, "_briefing_dialog", None)
        parent = getattr(dialog, "top", None)
        try:
            if parent is not None and not parent.winfo_exists():
                parent = None
        except tk.TclError:
            parent = None
        path = self.browse_project(parent=parent, offer_get_ready=False)
        if not path:
            return None
        cfg["project_path"] = path
        return True, "Project folder set."

    def _attach_inline_fix(self, issue, failure, cfg):
        """Give a preflight-failure issue an inline action so the user can fix it
        on the pre-launch screen and Launch at once (Julian). The fix mutates the
        SAME cfg dict the launch will use, so a re-check clears the issue and the
        change carries straight into the launch.

          Postgres without psycopg2 -> [Change to SQLite]
          room not in the project's ROOMS -> an inline "Rooms found" picker
          port in use -> [Re-check]
        """
        meta = core.issue_fix_for(failure)
        tag = meta.get("fix")
        if tag == "change_to_sqlite":
            def _to_sqlite(cfg=cfg):
                # Persist into the config being edited (the pre-launch screen is an
                # editor, not a preview): mutate the launch cfg AND write back to
                # the main form so the change sticks on launch OR cancel.
                cfg["db_mode"] = core.DB_MODE_NONE
                self.var["db_mode"].set(core.DB_MODE_NONE)
                self.apply_db_mode()
                return True, ("Switched to the oTree default database (SQLite). "
                              "Postgres and psycopg2 are no longer needed.")
            issue["fix"] = _to_sqlite
            issue["fix_label"] = meta.get("fix_label", "Change to SQLite")
        elif tag == "recheck":
            issue["fix"] = lambda: (True, "Re-checked.")
            issue["fix_label"] = meta.get("fix_label", "Re-check")
        elif tag == "pick_room":
            issue["kind"] = "room_pick"
            issue["rooms"] = meta.get("rooms", [])

            def _apply_room(room, cfg=cfg):
                # Persist the room into the config being edited (main form + cfg).
                cfg["room_name"] = room
                self.set_room(room)
                return True, "Room set to %r." % room
            issue["apply_room"] = _apply_room
            # Addendum: also offer to DEFINE the chosen room in the project via
            # the EXISTING safe append-block path (timestamped .bak, refuse-if-
            # present, revertible), so the chosen room becomes valid at launch.
            # Offered only when seats are used (the block defines the room from
            # the launcher's seat file) and the block is not there yet.
            room = cfg.get("room_name", "").strip()
            path = cfg.get("project_path", "").strip()
            if room and path and effective_seat_mode(cfg) != SEAT_NONE:
                state = inspect_settings(path)
                if state.get("readable") and not state.get("has_block"):
                    issue["add_room"] = room
                    issue["add_room_fix"] = self._add_room_via_block

    def _project_needs_block(self, cfg):
        """True when a seat-using launch would need the lab support block but the
        project's settings.py does not have it yet."""
        if effective_seat_mode(cfg) == SEAT_NONE:
            return False
        path = cfg["project_path"].strip()
        if not path or not os.path.isdir(path):
            return False
        state = inspect_settings(path)
        return bool(state.get("readable")) and not state.get("has_block")

    def _room_will_be_defined_by_block(self, cfg):
        """True when the lab support block is already in settings.py and a seat
        file will be written, so the block's ROOMS clause defines the chosen room
        at launch even though it is not in the project's static ROOMS. Used to
        clear the room-not-in-ROOMS blocker after the block is added."""
        if effective_seat_mode(cfg) == SEAT_NONE:
            return False
        path = cfg["project_path"].strip()
        if not path or not os.path.isdir(path):
            return False
        state = inspect_settings(path)
        return bool(state.get("has_block"))

    def _add_room_via_block(self):
        """Addendum: define the chosen room by appending the lab support block,
        reusing the SAME safe writer as "Add block to settings.py"
        (core.append_block via _get_ready_for_lab: timestamped .bak, refuse if the
        block is already present, revertible, explicit click only). At launch the
        block's ROOMS clause defines whatever room the config uses, so the chosen
        room becomes valid. Returns (ok, message) for the pre-launch fix note."""
        return self._get_ready_for_lab()

    def _show_before_launch(self, cfg):
        # ONE consolidated pre-launch screen (Julian): the launch summary PLUS
        # every issue found (hard blocks + fail-soft warnings, each with an inline
        # one-click fix or a short hint), and the action row. No chain of separate
        # warning/error dialogs, a clean setup is a single confirm. All host/room/
        # caution content comes from core.launch_briefing; the dialog only renders.
        # Carry the admin credentials into the briefing so the handoff is one
        # combined popup. Auto login (default ON) decides whether we ATTEMPT a real
        # form-login + cookie relay (dashboard opens already logged in) or just
        # open the login page; pre_auth reflects that intent so the credentials
        # block reads "should open logged in; if it still asks, type these".
        use_auto = bool(self.auto_login.get())
        cfg = dict(cfg)
        cfg["auto_login"] = use_auto
        # The briefing is recomputed on demand (get_briefing), so an inline fix on
        # the pre-launch screen, Change to SQLite, or picking a different room,
        # updates the caution bar and per-seat link in place.
        def get_briefing(cfg=cfg):
            return core.launch_briefing(cfg, self.lab_presets)

        pre_auth = use_auto
        # Keep a reference so the launch worker can drive this popup's handoff
        # banner with the REAL outcome (cookie vs manual vs failure) via
        # set_result. on_open_dashboard re-runs the real authenticated open for
        # the takeover "Click here" (the cookie relay is one-shot, so it does a
        # fresh form-login each time).
        self._briefing_dialog = LaunchBriefingDialog(
            self.root, self.fonts, get_briefing,
            on_okay=lambda: self._begin_launch(cfg),
            gather_issues=lambda: self.gather_launch_issues(cfg),
            needs_save=self._needs_save,
            on_save=self.save_as_new,
            on_view_block=self.show_block,
            admin_username=cfg["admin_username"],
            admin_password=cfg["admin_password"],
            pre_auth=pre_auth,
            on_open_dashboard=lambda: self._reopen_dashboard(cfg))

    def _begin_launch(self, cfg):
        if self.running:
            return
        self.inline_status.set("muted", "Launching...")
        self.running = True
        self._expand_log_for_launch()
        self.launch_button.configure(state="disabled", text="Launching...",
                                     bg=COLORS["accent_dark"])
        thread = threading.Thread(target=self._launch_worker, args=(cfg,), daemon=True)
        thread.start()

    def _expand_log_for_launch(self):
        """Grow the short activity log once a launch starts."""
        if not self._log_expanded:
            self._log_expanded = True
            try:
                self.log_text.configure(height=12)
                self.log_caption.configure(text="ACTIVITY LOG  ▾")
            except tk.TclError:
                pass

    def _project_folder_problem(self, cfg):
        """The project-folder must-fix message, or "" when the folder is fine.
        Same two conditions as always (none chosen / does not exist); split out
        of _hard_block_problems only so the pre-launch screen can attach its
        inline [Choose folder…] button to exactly this item."""
        path = cfg["project_path"].strip()
        if not path:
            return ("Choose your oTree project folder before launching (the folder that "
                    "holds settings.py).")
        if not os.path.isdir(path):
            return ('The project folder does not exist: "%s". Choose it again, or check '
                    "whether the drive is connected." % path)
        return ""

    def _hard_block_problems(self, cfg):
        """Reasons this config cannot be launched at all, in plain language.

        Only genuine blockers with no one-click fix live here (no project folder,
        an empty custom host, an unusable seat list). oTree-not-installed and the
        missing lab support block are handled as fail-soft warnings on the
        consolidated screen (the former via core.preflight, the latter with an
        inline "Add it for me"), so they are NOT repeated here."""
        problems = []
        folder_problem = self._project_folder_problem(cfg)
        if folder_problem:
            problems.append(folder_problem)
        if cfg["lab"] == LAB_CUSTOM and not cfg["custom_host"].strip():
            problems.append(
                "Custom host is selected in section 3 but the address box is empty. Type an "
                "address, or choose Small lab or Large lab.")

        # Seats are NEVER a hard block (Julian): no seat file / no default seats
        # falls back to the open (none) room. The ONLY genuine seat block is a
        # chosen FILE that DOES exist but holds labels oTree would reject.
        mode = cfg["seat_mode"]
        if mode == SEAT_FILE:
            seat_file = cfg["seat_file"].strip()
            if seat_file and os.path.isfile(seat_file):
                try:
                    bad = invalid_seats(read_seat_file(seat_file))
                except OSError:
                    bad = []
                if bad:
                    problems.append(
                        "The chosen participant file has labels oTree would reject "
                        "(only letters, numbers and underscores are allowed): %s"
                        % ", ".join(bad[:6]))
        elif mode in (SEAT_DEFAULT, SEAT_EDIT):
            seats = self._resolve_seats(cfg)
            bad = invalid_seats(seats)
            if bad:
                problems.append(
                    "oTree only accepts letters, numbers and underscores in a seat label. "
                    "These would be rejected: %s" % ", ".join(bad[:6]))
        return problems

    def _launch_worker(self, cfg):
        # _do_launch returns (ok, info): info is the open-dashboard URL on a real
        # success, or a plain-language failure reason otherwise. That result, the
        # REAL outcome, is what drives the handoff popup, so the takeover can
        # never claim "dashboard opening" when nothing actually started.
        result = (False, "Launch did not complete. See the activity log above.")
        try:
            result = self._do_launch(cfg) or result
        except Exception as error:  # keep the window alive whatever happens
            self.log("Unexpected error: %s: %s" % (type(error).__name__, error), "err")
            self._on_main(lambda: self.inline_status.set(
                "error", "Launch stopped: %s" % error))
            result = (False, "Launch stopped: %s" % error)
        finally:
            ok, info = result
            self._on_main(lambda: self._deliver_launch_result(ok, info))

    def _on_main(self, callback):
        """Run a callback on the GUI thread, unless the window has gone."""
        try:
            self.root.after(0, callback)
        except (tk.TclError, RuntimeError):
            pass

    def _deliver_launch_result(self, ok, info):
        """Reset the launch button and tell the briefing/takeover popup the REAL
        outcome, so its banner reflects success or failure honestly."""
        self._launch_finished()
        dialog = getattr(self, "_briefing_dialog", None)
        if dialog is not None:
            try:
                dialog.set_result(ok, info)
            except tk.TclError:
                pass

    def _launch_finished(self):
        self.running = False
        self.launch_button.configure(state="normal", text="Launch", bg=COLORS["accent"])

    def _reopen_dashboard(self, cfg):
        """Re-open the admin dashboard for the takeover 'Click here' link.

        Runs the same real authenticated open on a worker thread (the cookie relay
        is one-shot, so this does a fresh form-login) and reports the outcome to
        the activity log honestly."""
        def worker():
            result = core.open_dashboard_authenticated(
                core.AUTOLOGIN_HOST, cfg["port"], cfg["room_name"],
                cfg["admin_username"], cfg["admin_password"],
                auto_login=cfg.get("auto_login", True))
            if result["method"] == "cookie":
                self.log("Re-opened the dashboard already logged in: %s"
                         % result["monitor_url"], "ok")
            else:
                self.log("Re-opened the dashboard login page: %s (%s)"
                         % (result["monitor_url"], result["reason"]), "info")
        threading.Thread(target=worker, daemon=True).start()

    def _do_launch(self, cfg):
        path = cfg["project_path"].strip()

        self.log("-" * 60, "muted", prefix=False)
        self.log("Starting a run. No batch file is written or read.", "info")
        self.log("Working directory: %s" % path, "info")

        config_name = "session"
        if self.selected_index is not None:
            config_name = self.presets[self.selected_index].get("name", "session")
        try:
            label_file, note = self._prepare_label_file(cfg, config_name)
        except OSError as error:
            self.log("Could not write the seat file: %s" % error, "err")
            self._on_main(lambda: self.inline_status.set(
                "error", "Could not write the seat file: %s" % error))
            return False, "Could not write the seat file: %s" % error
        self.log(note, "info")
        if label_file and cfg["seat_mode"] != SEAT_FILE:
            self.log("    " + seat_preview(self._resolve_seats(cfg)), "muted", prefix=False)
        if label_file:
            state = inspect_settings(path)
            if state["rooms_after"]:
                self.log(state["message"], "warn")

        env = build_env(cfg, label_file=label_file)
        keys = launcher_env_keys(cfg, label_file=label_file)
        self.log("Environment variables set for this run: %s" % ", ".join(keys), "info")
        for key in keys:
            self.log("    %s = %s" % (key, describe_env_value(key, env.get(key, ""))),
                     "muted", prefix=False)
        if cfg["db_mode"] == DB_MODE_NONE:
            self.log("    DATABASE_URL is not set at all, so oTree uses its own default.",
                     "muted", prefix=False)
        if not label_file:
            self.log("    OTREE_LAB_LABEL_FILE and OTREE_LAB_ROOM_NAME are not set, so the block in "
                     "settings.py stays inert.", "muted", prefix=False)

        if cfg["resetdb"]:
            code = self._run_resetdb(path, env)
            if code is None:
                return False, "otree resetdb could not be started. See the log above."
            if code != 0:
                self.log("otree resetdb exited with code %d, so the server was not started."
                         % code, "err")
                self._on_main(lambda: self.inline_status.set(
                    "error", "otree resetdb failed (exit code %d). See the log above." % code))
                return False, ("otree resetdb failed (exit code %d). Nothing was started: "
                               "see the activity log." % code)
            self.log("otree resetdb finished with exit code 0.", "ok")
        else:
            self.log("Reset database is off, so otree resetdb was skipped.", "muted")

        if not self._start_server(cfg, path, env):
            return False, "The oTree server could not be started. See the activity log above."

        self._stamp_last_run(cfg)

        # Auto login (default ON, the "Auto login" tick on the oTree admin row):
        # open the admin room monitor already authenticated via a REAL form-login
        # + one-shot localhost cookie relay (core.open_dashboard_authenticated).
        # oTree ignores Basic Auth, so the old credentials-in-URL trick was dead;
        # this replays a real login and plants the session cookie. With Auto login
        # OFF, or if the login fails, it opens the plain login page and the
        # operator logs in by hand. Either way the launch briefing (which stays
        # open as the takeover) shows the admin username + password as the manual
        # fallback. Always opened on localhost (the operator's own machine); the
        # per-seat participant links keep the configured lab host, unchanged.
        use_auto = cfg.get("auto_login", True)
        result = None
        if cfg["open_browser"]:
            self.log("Waiting for the server to respond, then opening the dashboard.", "info")
            result = core.open_dashboard_authenticated(
                core.AUTOLOGIN_HOST, cfg["port"], cfg["room_name"],
                cfg["admin_username"], cfg["admin_password"], auto_login=use_auto)
            if result["method"] == "cookie":
                self.log("Opened the dashboard already logged in (auto-login: form-login + "
                         "cookie relay): %s" % result["monitor_url"], "ok")
            else:
                self.log("Opened the dashboard login page: %s" % result["monitor_url"], "info")
                self.log("    %s. Log in with the admin username and password shown in the popup."
                         % result["reason"], "muted", prefix=False)
        else:
            monitor_url = "http://%s:%s%s" % (
                core.AUTOLOGIN_HOST, cfg["port"], core.room_monitor_path(cfg["room_name"]))
            self.log("Open in browser is off. The page would be %s" % monitor_url, "muted")
            result = {"ok": True, "method": "manual", "monitor_url": monitor_url}

        self._on_main(lambda: self.inline_status.set(
            "ok", "Server started. Its window stays open; press Ctrl-C there to stop it."))
        self.log("Done. The server keeps running in its own window.", "ok")
        # The launch briefing popup is still open in its "launching" state; on
        # this real success it switches to the post-launch takeover banner, which
        # reports HONESTLY whether the dashboard opened logged in (cookie) or at
        # the login page (manual), via _deliver_launch_result.
        return True, result

    def _run_resetdb(self, path, env):
        command = resetdb_command()
        self.log("$ " + " ".join(command) + '     (answering "y" on stdin)', "cmd")
        kwargs = {}
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        try:
            process = subprocess.Popen(
                command, cwd=path, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                universal_newlines=True, bufsize=1, **kwargs)
        except OSError as error:
            self.log("Could not run otree resetdb: %s" % error, "err")
            self._on_main(lambda: self.inline_status.set(
                "error", "Could not run otree resetdb: %s" % error))
            return None
        try:
            process.stdin.write("y\n")
            process.stdin.flush()
            process.stdin.close()
        except (OSError, ValueError):
            pass
        for line in process.stdout:
            line = line.rstrip()
            if line:
                self.log(line, "out", prefix=False)
        return process.wait()

    def _start_server(self, cfg, path, env):
        spec = build_server_launch(cfg, path, env)
        self.log("Starting the server in a %s." % spec["description"], "info")
        self.log("$ " + " ".join(spec["cmd"][:2]) + (" ..." if len(spec["cmd"]) > 2 else ""),
                 "cmd")
        kwargs = {"cwd": path, "env": env}
        if spec["creationflags"]:
            kwargs["creationflags"] = spec["creationflags"]
        try:
            if spec["kind"] == "linux-background":
                process = subprocess.Popen(
                    spec["cmd"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    universal_newlines=True, bufsize=1, **kwargs)
                threading.Thread(target=self._pipe_to_log, args=(process,), daemon=True).start()
            else:
                subprocess.Popen(spec["cmd"], **kwargs)
        except OSError as error:
            self.log("Could not start otree prodserver: %s" % error, "err")
            self._on_main(lambda: self.inline_status.set(
                "error", "Could not start otree prodserver: %s" % error))
            return False
        self.log("otree prodserver started.", "ok")
        return True

    def _pipe_to_log(self, process):
        for line in process.stdout:
            line = line.rstrip()
            if line:
                self.log(line, "out", prefix=False)

    def _stamp_last_run(self, cfg):
        if self.selected_index is None or self.dirty:
            self.log("These settings are not a saved config, so no config was stamped. "
                     "Use Save as new to keep them.", "muted")
            return
        preset = self.presets[self.selected_index]
        preset["last_run"] = now_iso()
        try:
            save_store(self.presets, self.store_extra, self.store_path)
        except OSError as error:
            self.log("Could not update the last-run time: %s" % error, "warn")
            return
        self.log('Stamped "%s" as last run just now.' % preset.get("name", ""), "muted")
        self._on_main(self.refresh_sidebar)

    def on_close(self):
        self.root.destroy()


def _build_credentials_block(parent, fonts, username, password, pre_auth):
    """Self-contained admin username/password handoff block.

    Each value sits in a read-only field with its own Copy button; the password
    is masked with a Show toggle. Returns the (un-gridded/un-packed) frame so the
    caller places it. Used by BOTH the launch briefing and the post-launch
    takeover, so the credentials handoff looks and behaves identically wherever
    the operator needs it (round 7: the handoff now rides inside the briefing and
    is carried onto the takeover so it stays available when the browser asks)."""
    creds = tk.Frame(parent, bg=COLORS["card"], highlightthickness=1,
                     highlightbackground=COLORS["card_line"])
    inner = tk.Frame(creds, bg=COLORS["card"])
    inner.pack(fill="x", padx=12, pady=10)
    inner.columnconfigure(1, weight=1)
    intro = ("The dashboard should open already logged in. If the browser DOES ask, "
             "type or paste:") if pre_auth else "Log in to the dashboard with:"
    tk.Label(inner, text=intro, bg=COLORS["card"], fg=COLORS["muted"],
             font=fonts.small, anchor="w", justify="left", wraplength=340).grid(
        row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8))

    def copy(text, button):
        try:
            parent.clipboard_clear()
            parent.clipboard_append(text)
            button.configure(text="Copied")
            button.after(1200, lambda: button.configure(text="Copy"))
        except tk.TclError:
            pass

    tk.Label(inner, text="Username", bg=COLORS["card"], fg=COLORS["muted"],
             font=fonts.small).grid(row=1, column=0, sticky="w", padx=(0, 8))
    user_entry = tk.Entry(inner, font=fonts.mono_small, bg=COLORS["field_off"],
                          fg=COLORS["text"], relief="flat",
                          readonlybackground=COLORS["field_off"])
    user_entry.insert(0, username)
    user_entry.configure(state="readonly")
    user_entry.grid(row=1, column=1, sticky="ew", pady=(0, 5))
    user_copy = ttk.Button(inner, text="Copy", style="Slim.TButton", width=6)
    user_copy.configure(command=lambda: copy(username, user_copy))
    user_copy.grid(row=1, column=2, sticky="e", padx=(8, 0), pady=(0, 5))

    tk.Label(inner, text="Password", bg=COLORS["card"], fg=COLORS["muted"],
             font=fonts.small).grid(row=2, column=0, sticky="w", padx=(0, 8))
    pw_entry = tk.Entry(inner, font=fonts.mono_small, bg=COLORS["field_off"],
                        fg=COLORS["text"], relief="flat",
                        readonlybackground=COLORS["field_off"], show=MASK_CHAR)
    pw_entry.insert(0, password)
    pw_entry.configure(state="readonly")
    pw_entry.grid(row=2, column=1, sticky="ew")
    pw_btns = tk.Frame(inner, bg=COLORS["card"])
    pw_btns.grid(row=2, column=2, sticky="e", padx=(8, 0))
    shown = {"on": False}

    def toggle_pw():
        shown["on"] = not shown["on"]
        try:
            pw_entry.configure(show="" if shown["on"] else MASK_CHAR)
            pw_show.configure(text="Hide" if shown["on"] else "Show")
        except tk.TclError:
            pass

    pw_show = ttk.Button(pw_btns, text="Show", style="Slim.TButton", width=6,
                         command=toggle_pw)
    pw_show.pack(side="left")
    pw_copy = ttk.Button(pw_btns, text="Copy", style="Slim.TButton", width=6)
    pw_copy.configure(command=lambda: copy(password, pw_copy))
    pw_copy.pack(side="left", padx=(6, 0))
    return creds


class GetReadyDialog(object):
    """Proactive one-click 'get ready for the lab' prompt.

    Shown right after a not-lab-ready project is chosen. Minimal by default:
    a headline, one line, one primary button, a 'More information' link and a
    dismiss. 'More information' expands the explanation in place (no navigating
    away). The primary button appends the lab support block through the app callback,
    which reuses the same backup and refuse-to-append-twice guard as the manual
    button. Like the other modals it dims the launcher and grab_sets, but it is
    freely dismissible so a non-lab project chosen on purpose closes cleanly.
    """

    WRAP = 372

    def __init__(self, parent, fonts, on_ready):
        self.parent = parent
        self.fonts = fonts
        self.on_ready = on_ready
        self.expanded = False
        self.done = False

        top = self.top = tk.Toplevel(parent)
        top.title("Get ready for the lab")
        top.configure(bg=COLORS["card"])
        top.transient(parent)
        top.resizable(False, False)
        # Dim the launcher behind the popup. The shared helper keeps the shade
        # strictly BELOW this dialog and re-lifts the dialog on every map, so the
        # overlay can never grey out or swallow clicks meant for the dialog
        # (the macOS aqua freeze). Grab stays on the dialog, below.
        self.shade = _attach_shade(parent, top)

        body = tk.Frame(top, bg=COLORS["card"])
        body.pack(fill="both", expand=True, padx=22, pady=20)
        body.columnconfigure(0, weight=1)

        self.headline = tk.Label(
            body,
            text="This project is not set up for the lab yet.",
            bg=COLORS["card"], fg=COLORS["text"], font=fonts.bold,
            anchor="w", justify="left", wraplength=self.WRAP)
        self.headline.grid(row=0, column=0, sticky="ew")

        self.subline = tk.Label(
            body,
            text="One click gets it ready.",
            bg=COLORS["card"], fg=COLORS["muted"], font=fonts.small,
            anchor="w", justify="left", wraplength=self.WRAP)
        self.subline.grid(row=1, column=0, sticky="ew", pady=(7, 0))

        # 'More information' reveals this frame in place (hidden at first).
        self.info = tk.Frame(body, bg=COLORS["accent_soft"], highlightthickness=1,
                             highlightbackground=COLORS["card_line"])
        self.info.columnconfigure(0, weight=1)
        tk.Label(self.info, text=self._info_text(), bg=COLORS["accent_soft"],
                 fg=COLORS["text"], font=fonts.small, anchor="w", justify="left",
                 wraplength=self.WRAP - 22).grid(row=0, column=0, sticky="ew",
                                                 padx=11, pady=9)

        self.more = tk.Label(body, text="More information  ▾", bg=COLORS["card"],
                             fg=COLORS["muted"], font=fonts.small, cursor="hand2",
                             anchor="w")
        self.more.grid(row=3, column=0, sticky="w", pady=(12, 0))
        self.more.bind("<Button-1>", lambda _e: self._toggle_info())

        buttons = tk.Frame(body, bg=COLORS["card"])
        buttons.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        self.dismiss = ttk.Button(buttons, text="Not now", command=self._close)
        self.dismiss.pack(side="right")
        self.primary = tk.Button(
            buttons, text="OK, get ready for the lab", command=self._get_ready,
            font=fonts.bold, bg=COLORS["accent"], fg="#ffffff",
            activebackground=COLORS["accent_dark"], activeforeground="#ffffff",
            relief="flat", bd=0, padx=16, pady=6, cursor="hand2", highlightthickness=0)
        self.primary.pack(side="right", padx=(0, 8))

        top.protocol("WM_DELETE_WINDOW", self._close)
        top.bind("<Escape>", lambda _e: self._close())
        self._recenter()
        # The shared shade helper keeps the dialog lifted above the shade; the
        # grab goes on the dialog (never the shade) so clicks reach the dialog.
        _grab_modal(top)
        top.focus_set()

    def _info_text(self):
        return (
            "What it does: it appends a clearly-marked block to the END of settings.py "
            "that re-asserts what the lab needs, each value taken from the launcher at "
            "launch:\n"
            "  •  the lab room + seat board  (ROOMS + participant_label_file)\n"
            "  •  the lab database  (DATABASES)\n"
            "  •  the admin login  (ADMIN_USERNAME / ADMIN_PASSWORD)\n"
            "  •  the access level  (AUTH_LEVEL) and DEBUG / production\n\n"
            "A timestamped .bak copy of your settings.py is saved first. Every override "
            "is guarded by the launcher's environment variables, so with no lab "
            "environment set your project is byte-for-byte unchanged. It is fully "
            "revertible: delete from the banner line to the end of settings.py.")

    def _toggle_info(self):
        self.expanded = not self.expanded
        if self.expanded:
            self.info.grid(row=2, column=0, sticky="ew", pady=(11, 0))
            self.more.configure(text="Less information  ▴")
        else:
            self.info.grid_remove()
            self.more.configure(text="More information  ▾")
        self._recenter()

    def _get_ready(self):
        if self.done:
            self._close()
            return
        ok, message = self.on_ready()
        self.subline.configure(text=message,
                               fg=COLORS["ok"] if ok else COLORS["warn"])
        if ok:
            self.done = True
            self.primary.configure(text="Ready ✓", state="disabled",
                                   bg=COLORS["accent_dark"], cursor="arrow")
            self.dismiss.configure(text="Close")
        self._recenter()

    def _recenter(self):
        top = self.top
        top.update_idletasks()
        x = self.parent.winfo_rootx() + (self.parent.winfo_width() - top.winfo_width()) // 2
        y = self.parent.winfo_rooty() + (self.parent.winfo_height() - top.winfo_height()) // 3
        top.geometry("+%d+%d" % (max(x, 0), max(y, 0)))

    def _close(self):
        try:
            self.top.grab_release()
        except tk.TclError:
            pass
        try:
            self.top.destroy()
        except tk.TclError:
            pass
        _destroy_shade(self.shade)
        self.shade = None


class BlockDialog(object):
    """Show the settings.py block, offer to copy it, offer to add it.

    Adding it is the only thing in this app that writes into somebody else's
    project, which is why it takes an explicit click and a backup.
    """

    def __init__(self, parent, fonts, state, add_callback, log):
        self.parent = parent
        self.add_callback = add_callback
        self.log = log
        top = self.top = tk.Toplevel(parent)
        top.title("oTree lab support block")
        top.configure(bg=COLORS["card"])
        _attach_shade(parent, top)
        top.transient(parent)
        top.geometry("760x560")
        top.minsize(560, 420)

        body = tk.Frame(top, bg=COLORS["card"])
        body.pack(fill="both", expand=True, padx=16, pady=14)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)

        tk.Label(body, text="Paste this at the END of settings.py",
                 bg=COLORS["card"], fg=COLORS["text"], font=fonts.bold,
                 anchor="w").grid(row=0, column=0, sticky="ew")

        self.status = StatusLine(body, fonts.small)
        self.status.grid(row=1, column=0, sticky="ew", pady=(4, 8))
        self.status.set_wraplength(700)
        self._show_state(state)

        holder = tk.Frame(body, bg=COLORS["card_line"], highlightthickness=1,
                          highlightbackground=COLORS["card_line"])
        holder.grid(row=2, column=0, sticky="nsew")
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)
        self.text = tk.Text(holder, font=fonts.mono, wrap="none", bd=0,
                            highlightthickness=0, padx=8, pady=6,
                            bg="#ffffff", fg=COLORS["text"])
        self.text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=scroll.set)
        self.text.insert("1.0", LAB_BLOCK)
        self.text.configure(state="disabled")

        tk.Label(body,
                 text="With none of the launcher's variables set, this block does nothing, so "
                      "it is safe to leave in the project permanently.",
                 bg=COLORS["card"], fg=COLORS["muted"], font=fonts.small, anchor="w",
                 justify="left", wraplength=700).grid(row=3, column=0, sticky="ew",
                                                      pady=(8, 8))

        buttons = tk.Frame(body, bg=COLORS["card"])
        buttons.grid(row=4, column=0, sticky="ew")
        ttk.Button(buttons, text="Close", command=top.destroy).pack(side="right")
        self.add_button = ttk.Button(buttons, text="Add to settings.py", command=self._add)
        self.add_button.pack(side="right", padx=(0, 8))
        ttk.Button(buttons, text="Copy", command=self._copy).pack(side="right", padx=(0, 8))
        if state.get("has_block") or not state.get("readable"):
            self.add_button.state(["disabled"])

        top.bind("<Escape>", lambda _e: top.destroy())
        _grab_modal(top)

    def _show_state(self, state):
        if not state.get("readable"):
            self.status.set("muted", state.get("message", ""))
        elif not state.get("has_block"):
            self.status.set("error", state.get("message", ""))
        elif state.get("rooms_after") or not state.get("complete"):
            self.status.set("warn", state.get("message", ""))
        else:
            self.status.set("ok", state.get("message", ""))

    def _copy(self):
        self.top.clipboard_clear()
        self.top.clipboard_append(LAB_BLOCK)
        self.log("Copied the oTree lab support block to the clipboard.", "muted")

    def _add(self):
        if self.add_callback():
            self.add_button.state(["disabled"])
            self.status.set("ok", "Added. A timestamped copy of the old settings.py is "
                                  "beside it; both paths are in the log.")


class NameDialog(object):
    """Ask for the name of a new config, and explain why a new one is needed."""

    def __init__(self, parent, fonts, suggestion, is_free, author=None, researchers=None):
        self.result = None
        self.author = None
        self.is_free = is_free
        self._researchers = list(researchers or [])
        top = self.top = tk.Toplevel(parent)
        top.title("Save as new config")
        top.configure(bg=COLORS["card"])
        _attach_shade(parent, top)
        top.transient(parent)
        top.resizable(False, False)

        body = tk.Frame(top, bg=COLORS["card"])
        body.pack(fill="both", expand=True, padx=18, pady=16)
        tk.Label(body, text="Save these settings as a new config",
                 bg=COLORS["card"], fg=COLORS["text"], font=fonts.bold, anchor="w").pack(fill="x")
        tk.Label(body,
                 text="Saved configs are never changed. This adds a new one and leaves every "
                      "existing config exactly as it is.",
                 bg=COLORS["card"], fg=COLORS["muted"], font=fonts.small, anchor="w",
                 justify="left", wraplength=380).pack(fill="x", pady=(4, 12))

        tk.Label(body, text="Config name", bg=COLORS["card"], fg=COLORS["muted"],
                 font=fonts.small, anchor="w").pack(fill="x")
        self.var = tk.StringVar(top, value=suggestion)
        entry = ttk.Entry(body, textvariable=self.var, width=44)
        entry.pack(fill="x", pady=(2, 0))

        tk.Label(body, text="Researcher", bg=COLORS["card"], fg=COLORS["muted"],
                 font=fonts.small, anchor="w").pack(fill="x", pady=(10, 0))
        # The shared researcher roster: type a new name or pick a saved one. The
        # chosen name feeds the same roster the create-database dialog uses.
        self.author_var = tk.StringVar(
            top, value=default_author() if author is None else author)
        author_entry = ttk.Combobox(body, textvariable=self.author_var, width=42,
                                    values=self._researchers)
        author_entry.pack(fill="x", pady=(2, 0))

        self.error = tk.Label(body, text="", bg=COLORS["card"], fg=COLORS["error"],
                              font=fonts.small, anchor="w", justify="left", wraplength=380)
        self.error.pack(fill="x", pady=(6, 0))

        buttons = tk.Frame(body, bg=COLORS["card"])
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="Cancel", command=self.cancel).pack(side="right")
        ttk.Button(buttons, text="Save", command=self.confirm).pack(side="right", padx=(0, 8))

        top.bind("<Return>", lambda _e: self.confirm())
        top.bind("<Escape>", lambda _e: self.cancel())
        entry.focus_set()
        entry.selection_range(0, "end")
        top.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - top.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - top.winfo_height()) // 3
        top.geometry("+%d+%d" % (max(x, 0), max(y, 0)))
        _grab_modal(top)
        parent.wait_window(top)

    def confirm(self):
        name = self.var.get().strip()
        if not name:
            self.error.configure(text="Type a name for this config.")
            return
        if not self.is_free(name):
            self.error.configure(
                text='There is already a config called "%s". Pick another name; existing '
                     "configs are never overwritten." % name)
            return
        self.result = name
        self.author = self.author_var.get().strip()
        self.top.destroy()

    def cancel(self):
        self.result = None
        self.top.destroy()


def _center_on(parent, top, divisor=3):
    top.update_idletasks()
    x = parent.winfo_rootx() + (parent.winfo_width() - top.winfo_width()) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - top.winfo_height()) // divisor
    top.geometry("+%d+%d" % (max(x, 0), max(y, 0)))


def _modal_close(top):
    try:
        top.grab_release()
    except tk.TclError:
        pass
    try:
        top.destroy()
    except tk.TclError:
        pass


def _wait_viewable(top, timeout=3.0):
    """Wait until ``top`` is mapped, but never block forever.

    ``top.wait_visibility()`` blocks until the window becomes viewable, which is
    the normal, quick case for a modal whose master is on screen. But a
    transient Toplevel of a *withdrawn* master never becomes viewable on
    macOS/aqua, so wait_visibility() there would hang the whole app with no
    window ever appearing. We poll ``winfo_viewable`` with a deadline instead:
    for an ordinary modal this returns as soon as the window maps (same effect
    as wait_visibility), and for one that is never going to map it simply falls
    through after the timeout rather than freezing.
    """
    end = time.time() + timeout
    while time.time() < end:
        try:
            if top.winfo_viewable():
                return
            top.update()
        except tk.TclError:
            return


def _grab_modal(top):
    """Take the modal input grab reliably across platforms.

    On macOS (aqua) ``grab_set()`` on a Toplevel that is not yet viewable can
    silently fail; the dialog then holds no grab the user can reach and the whole
    app looks frozen. So on darwin we first force the window realised, visible,
    raised and focused, then grab it. On Windows/Linux the historical plain
    ``grab_set()`` is kept (the dim shade already handles stacking there).

    Every step is guarded, a modal that cannot grab is far better than a crash,
    and this is the single choke-point every launcher modal goes through, so no
    dialog can leak a broken grab. The visibility wait is bounded (see
    ``_wait_viewable``) so a window that will never map cannot hang the app.
    """
    if sys.platform == "darwin":
        try:
            top.update_idletasks()
        except tk.TclError:
            pass
        _wait_viewable(top)
        for step in (top.lift, top.focus_force):
            try:
                step()
            except tk.TclError:
                pass
    try:
        top.grab_set()
    except tk.TclError:
        pass


def _make_shade(parent, alpha=0.45):
    """A borderless, semi-transparent dark overlay sized over the main window.

    The plain-Tk way to dim the launcher behind a modal so the dialog stands
    out. Returns the Toplevel, or None if the platform refuses the alpha /
    override tricks (in which case the modal simply shows without dimming).
    """
    try:
        shade = tk.Toplevel(parent)
        shade.overrideredirect(True)
        shade.configure(bg="#000000")
        shade.attributes("-alpha", alpha)
        parent.update_idletasks()
        shade.geometry("%dx%d+%d+%d" % (
            parent.winfo_width(), parent.winfo_height(),
            parent.winfo_rootx(), parent.winfo_rooty()))
        shade.transient(parent)
        return shade
    except tk.TclError:
        return None


def _destroy_shade(shade):
    if shade is not None:
        try:
            shade.destroy()
        except tk.TclError:
            pass


def _attach_shade(parent, top, alpha=0.45):
    """Dim the main window behind modal ``top`` and tie the overlay's lifetime
    to it: the shade is removed automatically when ``top`` is destroyed, so no
    close path has to know about it. Shared by every launcher modal.

    The shade MUST sit strictly BELOW the dialog, and the modal grab MUST be on
    the dialog (never the shade). If a semi-transparent overlay ends up stacked
    ABOVE the dialog it both greys the dialog out and swallows every click meant
    for it (the dialog looks frozen). On macOS (aqua) an overrideredirect /
    transient Toplevel can jump to the top of the stacking order, so the dialog
    is re-lifted above the shade every time it is mapped, not just once at
    creation. This is cross-platform-correct: the re-lift is a harmless no-op on
    Windows and X11 (where the initial lower already keeps the shade underneath),
    so there is no per-platform branch and the Windows lab path is unchanged.

    macOS (aqua) is the exception: the dim-shade overlay is itself what freezes
    the app there. An overrideredirect/transient Toplevel plus a grab taken
    before the dialog is viewable can leave the shade stacked ABOVE the dialog,
    greying it out and swallowing every click, while the unreachable grab blocks
    the main window too, so one stuck modal freezes everything. The shade is
    purely cosmetic, so on darwin we skip it entirely and rely on the reliable
    grab in ``_grab_modal`` to keep the dialog fully modal and interactive.
    Windows/Linux keep the nicer dimmed look unchanged.
    """
    if sys.platform == "darwin":
        return None
    shade = _make_shade(parent, alpha)
    if shade is None:
        return None

    def _restack(event=None, shade=shade, top=top):
        # Ignore Map events bubbling from child widgets; only react to the dialog.
        if event is not None and getattr(event, "widget", None) is not top:
            return
        try:
            shade.lower(top)   # shade just beneath the dialog...
        except tk.TclError:
            pass
        try:
            top.lift()         # ...and the dialog above everything, incl. the shade
        except tk.TclError:
            pass

    _restack()
    # Re-assert the order once the dialog is realised (after its body is built,
    # geometry set and grab taken) and on every subsequent map, so aqua cannot
    # leave the shade on top.
    top.bind("<Map>", _restack, add="+")
    try:
        top.after_idle(_restack)
    except tk.TclError:
        pass

    def _cleanup(event, shade=shade, top=top):
        if event.widget is top:
            _destroy_shade(shade)

    top.bind("<Destroy>", _cleanup, add="+")
    return shade


class PreflightWarningDialog(object):
    """The pre-launch preflight gate popup (checks 1–4).

    Shown only when one or more of core.preflight's fast checks failed. It lists
    each failed check's message (database, port, project/room, oTree) and offers
    two choices: "Launch anyway" (proceeds, nothing here hard-blocks) or
    "Cancel" (back to the launcher). It decides nothing itself; on "Launch
    anyway" it calls the callback the app passes."""

    def __init__(self, parent, fonts, failures, on_launch_anyway):
        self.on_launch_anyway = on_launch_anyway
        top = self.top = tk.Toplevel(parent)
        top.title("Pre-launch checks")
        top.configure(bg=COLORS["card"])
        _attach_shade(parent, top)
        top.transient(parent)
        top.resizable(False, False)

        body = tk.Frame(top, bg=COLORS["card"])
        body.pack(fill="both", expand=True, padx=20, pady=18)

        count = len(failures)
        tk.Label(body,
                 text="%d pre-launch check%s did not pass"
                      % (count, "" if count == 1 else "s"),
                 bg=COLORS["card"], fg=COLORS["text"], font=fonts.bold, anchor="w",
                 justify="left", wraplength=440).pack(fill="x")
        tk.Label(body,
                 text="These are warnings, not blocks. Review them, then launch "
                      "anyway or cancel and fix them first.",
                 bg=COLORS["card"], fg=COLORS["muted"], font=fonts.small, anchor="w",
                 justify="left", wraplength=440).pack(fill="x", pady=(4, 12))

        for failure in failures:
            item = tk.Frame(body, bg=COLORS["warn_soft"], highlightthickness=1,
                            highlightbackground=COLORS["card_line"])
            item.pack(fill="x", pady=(0, 8))
            tk.Label(item, text=failure.get("message", ""), bg=COLORS["warn_soft"],
                     fg=COLORS["warn"], font=fonts.body, anchor="w", justify="left",
                     wraplength=420).pack(fill="x", padx=10, pady=(7, 2))
            detail = str(failure.get("detail", "")).strip()
            if detail:
                tk.Label(item, text=detail, bg=COLORS["warn_soft"], fg=COLORS["muted"],
                         font=fonts.small, anchor="w", justify="left",
                         wraplength=420).pack(fill="x", padx=10, pady=(0, 7))

        buttons = tk.Frame(body, bg=COLORS["card"])
        buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(buttons, text="Cancel", command=self._cancel).pack(side="right")
        launch = tk.Button(buttons, text="Launch anyway", command=self._launch,
                           font=fonts.bold, bg=COLORS["accent"], fg="#ffffff",
                           activebackground=COLORS["accent_dark"], activeforeground="#ffffff",
                           relief="flat", padx=12, pady=5, cursor="hand2")
        launch.pack(side="right", padx=(0, 8))

        top.bind("<Escape>", lambda _e: self._cancel())
        top.bind("<Return>", lambda _e: self._launch())
        _center_on(parent, top)
        launch.focus_set()
        try:
            _grab_modal(top)
        except tk.TclError:
            pass

    def _launch(self):
        _modal_close(self.top)
        self.on_launch_anyway()

    def _cancel(self):
        _modal_close(self.top)


class SaveBeforeLaunchDialog(object):
    """Small pre-launch prompt shown when the on-screen config is dirty: offer to
    save it as a new config before launching. Three choices, save and launch,
    launch without saving, or cancel. It decides nothing itself; it calls the
    callbacks the app passes."""

    def __init__(self, parent, fonts, on_save, on_launch):
        self.on_save = on_save
        self.on_launch = on_launch
        top = self.top = tk.Toplevel(parent)
        top.title("Save before launching?")
        top.configure(bg=COLORS["card"])
        _attach_shade(parent, top)
        top.transient(parent)
        top.resizable(False, False)

        body = tk.Frame(top, bg=COLORS["card"])
        body.pack(fill="both", expand=True, padx=20, pady=18)
        tk.Label(body, text="Before launching, save this config as new?",
                 bg=COLORS["card"], fg=COLORS["text"], font=fonts.bold, anchor="w",
                 justify="left", wraplength=380).pack(fill="x")
        tk.Label(body,
                 text="These settings differ from the saved config. Saving keeps them as a new "
                      "config; you can also launch this once without saving.",
                 bg=COLORS["card"], fg=COLORS["muted"], font=fonts.small, anchor="w",
                 justify="left", wraplength=380).pack(fill="x", pady=(4, 14))

        buttons = tk.Frame(body, bg=COLORS["card"])
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Cancel", command=self._cancel).pack(side="right")
        ttk.Button(buttons, text="Launch without saving",
                   command=self._launch).pack(side="right", padx=(0, 8))
        save = tk.Button(buttons, text="Save and launch", command=self._save,
                         font=fonts.bold, bg=COLORS["accent"], fg="#ffffff",
                         activebackground=COLORS["accent_dark"], activeforeground="#ffffff",
                         relief="flat", padx=12, pady=5, cursor="hand2")
        save.pack(side="right", padx=(0, 8))

        top.bind("<Escape>", lambda _e: self._cancel())
        top.bind("<Return>", lambda _e: self._save())
        _center_on(parent, top)
        save.focus_set()
        try:
            _grab_modal(top)
        except tk.TclError:
            pass

    def _save(self):
        _modal_close(self.top)
        self.on_save()

    def _launch(self):
        _modal_close(self.top)
        self.on_launch()

    def _cancel(self):
        _modal_close(self.top)


class LaunchBriefingDialog(object):
    """The pre-launch instructions/confirmation popup (principle 12 + Feature 1).

    Layout, top to bottom, the same in every state: the issues (every MUST-FIX
    card first, each with an inline fix button where one exists, then the
    warning cards; scrollable when tall; no banner), the minimal "What will
    launch" card (top line = the Lab with an (i) that expands the lab / Server /
    Room details; then what to open on the participant computers, where the
    study room shows only its shortcut chip and reveals the per-seat link on
    hover, and any other room shows its link; the dashboard login sits behind a
    collapsed expander), then the bottom bar, the
    ONLY place a launch is offered ("Ready to launch" + Launch / the highlighted
    "N warnings" + Launch anyway callout / a disabled Launch while a must-fix
    stands).

    Renders the dict from core.launch_briefing: the chosen lab and host, the
    exact per-seat link the lab machines open, a warning when the room is not
    the study room the desktop shortcuts point at, and a bright red caution bar
    when the run is on the shared shared lab database. The server starts only
    when OKAY is pressed (on_okay). This dialog makes no launch decisions; it
    only renders what core computed.
    """

    def __init__(self, parent, fonts, briefing, on_okay,
                 gather_issues=None, needs_save=None, on_save=None, on_view_block=None,
                 admin_username="", admin_password="", pre_auth=False, takeover_url="",
                 on_open_dashboard=None):
        # ``briefing`` is either a static dict or a zero-arg callable returning a
        # fresh briefing. The callable form lets an inline fix (Change to SQLite,
        # pick a room) rebuild the caution bar and per-seat link in place.
        self.get_briefing = briefing if callable(briefing) else (lambda: briefing)
        self.on_okay = on_okay
        self.gather_issues = gather_issues or (lambda: [])
        self.needs_save = needs_save or (lambda: False)
        self.on_save = on_save
        self.on_view_block = on_view_block
        self.parent = parent
        self.fonts = fonts
        self.admin_username = admin_username
        self.admin_password = admin_password
        self.pre_auth = pre_auth
        self.takeover_url = takeover_url
        self.on_open_dashboard = on_open_dashboard
        self._issues = []
        self._fix_note = None
        self._launching = False
        # The two "What will launch" reveals (the lab (i) details, the dashboard
        # login) start COLLAPSED, and keep their state across the in-place
        # re-renders an inline fix / re-check triggers.
        self._info_open = False
        self._creds_open = False
        # The per-seat link hover card under the study-room chip (hidden by default).
        self.room_chip = None
        self.copy_link_button = None
        self._link_reveal = None
        self._reveal_after = None
        top = self.top = tk.Toplevel(parent)
        top.title("Before you launch")
        top.configure(bg=COLORS["card"])
        _attach_shade(parent, top)
        top.transient(parent)
        top.resizable(False, False)

        body = tk.Frame(top, bg=COLORS["card"])
        body.pack(fill="both", expand=True, padx=20, pady=18)
        # A steady width, so the dialog does not jump as issues come and go.
        body.columnconfigure(0, weight=1, minsize=self.WRAP + 20)

        # ONE consolidated pre-launch screen. Fixed layout rows so the summary,
        # issues, save affordance and action row can each be rebuilt in place
        # (after an inline fix or a re-check) without disturbing the others:
        #   row 0  issues: MUST-FIX cards first, then the warning cards. No banner
        #          and no "launch anyway" here. Scrolls when the list is tall.
        #   row 1  caution bar + minimal "What will launch" card (rebuildable)
        #   row 2  save affordance (rebuildable)
        #   row 3  separator + the state-dependent bottom bar (rebuildable): the
        #          ONLY place a launch / "Launch anyway" is offered.
        # The issue cards live in ``issues_frame``, an inner frame inside a canvas,
        # so a tall list (5+ issues) scrolls instead of overflowing a small
        # screen. A short list (0-2 issues) is shown in full with no scrollbar.
        self._issues_outer = tk.Frame(body, bg=COLORS["card"])
        self._issues_outer.grid(row=0, column=0, sticky="ew")
        self._issues_outer.columnconfigure(0, weight=1)
        self._issues_canvas = tk.Canvas(self._issues_outer, bg=COLORS["card"], height=1,
                                        width=self.WRAP + 20, highlightthickness=0, bd=0)
        self._issues_canvas.grid(row=0, column=0, sticky="ew")
        self._issues_scroll = ttk.Scrollbar(self._issues_outer, orient="vertical",
                                            command=self._issues_canvas.yview)
        self._issues_canvas.configure(yscrollcommand=self._issues_scroll.set)
        self.issues_frame = tk.Frame(self._issues_canvas, bg=COLORS["card"])
        self.issues_frame.columnconfigure(0, weight=1)
        self._issues_window = self._issues_canvas.create_window(
            (0, 0), window=self.issues_frame, anchor="nw")
        self._issues_canvas.bind(
            "<Configure>",
            lambda e: self._issues_canvas.itemconfigure(self._issues_window, width=e.width))
        self._issues_scrollable = False
        # Bound on the toplevel: every child has it in its bindtags, so the wheel
        # works wherever the pointer is (MouseWheel = Windows/macOS, 4/5 = X11).
        top.bind("<MouseWheel>", self._on_wheel)
        top.bind("<Button-4>", lambda _e: self._scroll_issues(-2))
        top.bind("<Button-5>", lambda _e: self._scroll_issues(2))

        self.summary_frame = tk.Frame(body, bg=COLORS["card"])
        self.summary_frame.grid(row=1, column=0, sticky="ew")
        self.summary_frame.columnconfigure(0, weight=1)

        self.save_frame = tk.Frame(body, bg=COLORS["card"])
        self.save_frame.grid(row=2, column=0, sticky="ew")
        self.save_frame.columnconfigure(0, weight=1)

        self.action = tk.Frame(body, bg=COLORS["card"])
        self.action.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        self.action.columnconfigure(1, weight=1)

        self._render_summary()
        self._render_issues()
        self._render_save()
        self._render_action()

        top.bind("<Escape>", lambda _e: self._close())
        _center_on(parent, top)
        try:
            _grab_modal(top)
        except tk.TclError:
            pass

    def _render_summary(self):
        """(Re)build the caution bar + launch summary from a FRESH briefing, so an
        inline fix (Change to SQLite clears the shared-db caution; picking a room
        changes the per-seat link) is reflected without reopening the dialog."""
        for child in list(self.summary_frame.winfo_children()):
            child.destroy()
        briefing = self.get_briefing()
        r = 0
        if briefing.get("caution"):
            bar = tk.Frame(self.summary_frame, bg=COLORS["error"], highlightthickness=0)
            bar.grid(row=r, column=0, sticky="ew", pady=(6, 10))
            bar.columnconfigure(1, weight=1)
            tk.Label(bar, text="▲", bg=COLORS["error"], fg="#ffffff",
                     font=self.fonts.bold).grid(row=0, column=0, sticky="n", padx=(12, 0), pady=9)
            tk.Label(bar, text=briefing.get("caution_text", ""), bg=COLORS["error"],
                     fg="#ffffff", font=self.fonts.bold, anchor="w", justify="left",
                     wraplength=self.WRAP - 30).grid(row=0, column=1, sticky="ew",
                                                     padx=(8, 12), pady=9)
            r += 1
        tk.Label(self.summary_frame, text="WHAT WILL LAUNCH", bg=COLORS["card"],
                 fg=COLORS["faint"], font=self.fonts.small_bold,
                 anchor="w").grid(row=r, column=0, sticky="ew", pady=(2, 4))
        r += 1
        card = tk.Frame(self.summary_frame, bg=COLORS["card"], highlightthickness=1,
                        highlightbackground=COLORS["card_line"])
        card.grid(row=r, column=0, sticky="ew", pady=(0, 10))
        card.columnconfigure(0, weight=1)
        summary = tk.Frame(card, bg=COLORS["card"])
        summary.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 0))
        summary.columnconfigure(0, weight=1)
        self._build_summary(summary, self.fonts, briefing,
                            self.admin_username, self.admin_password, self.pre_auth)

    # Text wrap width (px) shared by the issue rows and the summary.
    WRAP = 500

    def _build_summary(self, summary, fonts, briefing, admin_username, admin_password, pre_auth):
        """The MINIMAL launch summary, the same in every state.

        Top line: the LAB name with an (i) icon on the right; the (i) expands, in
        place, what that lab is plus the Server and Room. Below: what to open on
        the participant computers. For the study room the desktop shortcuts
        already point at, that is just the shortcut chip, and the per-seat link
        shows only while the pointer is over the chip. Any other room has no
        shortcut, so its link is shown. The dashboard login sits behind its own
        collapsed expander."""
        sr = 0
        self._hide_link_reveal()
        self.copy_link_button = None
        self.room_chip = None

        # 1. Top line: the Lab, with the (i) that folds in Server + Room.
        head = tk.Frame(summary, bg=COLORS["card"])
        head.grid(row=sr, column=0, sticky="ew", pady=(0, 8))
        head.columnconfigure(0, weight=1)
        sr += 1
        tk.Label(head, text=briefing.get("lab_label", ""), bg=COLORS["card"],
                 fg=COLORS["text"], font=fonts.subhead, anchor="w").grid(
            row=0, column=0, sticky="w")
        self.info_icon = self._info_icon(head)
        self.info_icon.grid(row=0, column=1, sticky="e")
        if self._info_open:
            info = tk.Frame(summary, bg=COLORS["field_off"], highlightthickness=1,
                            highlightbackground=COLORS["card_line"])
            info.grid(row=sr, column=0, sticky="ew", pady=(0, 8))
            info.columnconfigure(1, weight=1)
            sr += 1
            tk.Label(info, text=self._lab_about(briefing), bg=COLORS["field_off"],
                     fg=COLORS["muted"], font=fonts.small, anchor="w", justify="left",
                     wraplength=self.WRAP - 50).grid(row=0, column=0, columnspan=2,
                                                     sticky="ew", padx=10, pady=(7, 4))
            server = briefing.get("host", "")
            if server and briefing.get("port"):
                server = "%s:%s" % (server, briefing.get("port"))
            room_text = briefing.get("room", "")
            if room_text and briefing.get("open_room"):
                room_text += "  (open room, no seat list)"
            fr = 1
            for key, value in (("Server", server), ("Room", room_text)):
                if not value:
                    continue
                tk.Label(info, text=key, bg=COLORS["field_off"], fg=COLORS["faint"],
                         font=fonts.small, anchor="w", width=8).grid(
                    row=fr, column=0, sticky="w", padx=(10, 0), pady=1)
                tk.Label(info, text=value, bg=COLORS["field_off"], fg=COLORS["muted"],
                         font=fonts.mono_small, anchor="w").grid(
                    row=fr, column=1, sticky="w", pady=1)
                fr += 1
            tk.Frame(info, bg=COLORS["field_off"], height=6).grid(row=fr, column=0)

        # 2. What to open on the participant computers. An open room has no seat
        # label, so its link is the plain room link.
        if briefing.get("open_room"):
            link_label = "Each participant computer opens"
            template = briefing.get("example_link", "")
        else:
            link_label = "Per-seat link (replace SEAT with the seat number)"
            template = briefing.get("link_template", "")
        if briefing.get("has_participant_links", False):
            # The study room: the shortcut is already on the PCs, so the link is
            # HIDDEN. It appears only while the pointer is over the chip (and a
            # click on the chip copies it).
            line = tk.Frame(summary, bg=COLORS["card"])
            line.grid(row=sr, column=0, sticky="w", pady=(0, 8))
            sr += 1
            tk.Label(line, text="Open", bg=COLORS["card"], fg=COLORS["muted"],
                     font=fonts.body).pack(side="left")
            chip = self.room_chip = tk.Label(
                line, text=briefing.get("shortcut_name", ""), bg=COLORS["accent_soft"],
                fg=COLORS["accent"], font=fonts.bold, padx=9, pady=3, highlightthickness=1,
                highlightbackground=COLORS["accent"], cursor="hand2")
            chip.pack(side="left", padx=7)
            tk.Label(line, text="on each participant computer.", bg=COLORS["card"],
                     fg=COLORS["muted"], font=fonts.body).pack(side="left")
            if template:
                chip.bind("<Enter>", lambda _e, t=template, l=link_label:
                          self._schedule_link_reveal(t, l))
                chip.bind("<Leave>", lambda _e: self._hide_link_reveal())
                chip.bind("<Button-1>", lambda _e, t=template: self._copy_link(t))
        else:
            # Non-study room: no shortcut exists, so the link IS shown. State the
            # room, give the one-per-seat link to open on each computer, and note
            # the study-room alternative (do NOT claim the shortcuts "will not
            # match", the link still works).
            warn = tk.Frame(summary, bg=COLORS["warn_soft"], highlightthickness=1,
                            highlightbackground=COLORS["card_line"])
            warn.grid(row=sr, column=0, sticky="ew", pady=(0, 8))
            sr += 1
            warn.columnconfigure(0, weight=1)
            tk.Label(warn, bg=COLORS["warn_soft"], fg=COLORS["warn"], font=fonts.small,
                     anchor="w", justify="left", wraplength=self.WRAP - 50,
                     text=("This session uses the room %r. Open the link below on each "
                           "participant computer, one per seat. Or use the %r room to use "
                           "the shortcuts already on the participant PC."
                           % (briefing.get("room", ""),
                              briefing.get("default_room", "study")))
                     ).grid(row=0, column=0, sticky="ew", padx=10, pady=7)
            if template:
                # Label + Copy on one line, then the link at FULL width below so
                # it is readable without scrolling the field.
                linkrow = tk.Frame(summary, bg=COLORS["card"])
                linkrow.grid(row=sr, column=0, sticky="ew", pady=(0, 8))
                linkrow.columnconfigure(0, weight=1)
                sr += 1
                tk.Label(linkrow, text=link_label, bg=COLORS["card"], fg=COLORS["muted"],
                         font=fonts.small, anchor="w").grid(row=0, column=0, sticky="ew")
                self.copy_link_button = ttk.Button(
                    linkrow, text="Copy link", style="Slim.TButton",
                    command=lambda t=template: self._copy_link(t))
                self.copy_link_button.grid(row=0, column=1, padx=(6, 0))
                _copyable_line(linkrow, fonts, template).grid(
                    row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        # Dashboard login: collapsed by default, so the username/password are not
        # on screen until asked for (the password stays masked behind Show).
        if admin_username or admin_password:
            self._expander(summary, sr, "Dashboard login (if the browser asks)", "_creds_open")
            sr += 1
            if self._creds_open:
                _build_credentials_block(summary, fonts, admin_username, admin_password,
                                         pre_auth).grid(row=sr, column=0, sticky="ew",
                                                        pady=(2, 6))
                sr += 1
        tk.Frame(summary, bg=COLORS["card"], height=6).grid(row=sr, column=0)

    def _lab_about(self, briefing):
        """One plain sentence on what the chosen lab is (from the briefing only)."""
        name = briefing.get("lab_label", "") or "This lab"
        count = briefing.get("seat_count") or 0
        if briefing.get("open_room"):
            seats = "Open room: any computer can join, there is no seat list."
        elif briefing.get("seat_mode") == "file":
            seats = "%d seat labels, from your own seat file." % count
        else:
            seats = "%d participant seats." % count
        return ("%s. The oTree server for this session runs on the server below, "
                "and the participant computers join the room below. %s" % (name, seats))

    def _info_icon(self, parent):
        """The (i) on the lab line: a drawn circle (no glyph a font could lack).
        A click expands the lab / Server / Room details in place, and the state
        survives the summary being rebuilt after an inline fix / re-check."""
        size = 20
        icon = tk.Canvas(parent, width=size, height=size, bg=COLORS["card"],
                         highlightthickness=0, bd=0, cursor="hand2")
        fill = COLORS["accent"] if self._info_open else COLORS["card"]
        ink = "#ffffff" if self._info_open else COLORS["accent"]
        icon.create_oval(2, 2, size - 2, size - 2, outline=COLORS["accent"], fill=fill, width=1)
        icon.create_text(size // 2, size // 2, text="i", fill=ink, font=self.fonts.small_bold)

        def _toggle(_event=None):
            self._info_open = not self._info_open
            self._render_summary()
            try:
                self.top.update_idletasks()
            except tk.TclError:
                pass
        icon.bind("<Button-1>", _toggle)
        return icon

    def _schedule_link_reveal(self, link, label, delay=120):
        self._hide_link_reveal()
        try:
            self._reveal_after = self.top.after(
                delay, lambda: self._show_link_reveal(link, label))
        except tk.TclError:
            self._reveal_after = None

    def _show_link_reveal(self, link, label):
        """The hover card under the room chip: "Open this desktop shortcut on each
        PC" + the per-seat link, for reference. It is a frame PLACED over the
        dialog's own content (not a new Toplevel), so it cannot disturb the modal
        grab or the window stacking on any platform, and the layout never jumps."""
        self._reveal_after = None
        chip = self.room_chip
        try:
            if chip is None or not chip.winfo_exists() or self._link_reveal is not None:
                return
            top = self.top
            card = tk.Frame(top, bg="#2c3440", highlightthickness=1,
                            highlightbackground="#2c3440")
            tk.Label(card, text="Open this desktop shortcut on each PC.", bg="#2c3440",
                     fg="#f2f5f9", font=self.fonts.small_bold, anchor="w").pack(
                fill="x", padx=9, pady=(6, 1))
            tk.Label(card, text=label + ", for reference. Click the chip to copy it.",
                     bg="#2c3440", fg="#c3cad4", font=self.fonts.small, anchor="w").pack(
                fill="x", padx=9)
            tk.Label(card, text=link, bg="#2c3440", fg="#f2f5f9", font=self.fonts.mono_small,
                     anchor="w").pack(fill="x", padx=9, pady=(3, 7))
            card.update_idletasks()
            width, height = card.winfo_reqwidth(), card.winfo_reqheight()
            x = chip.winfo_rootx() - top.winfo_rootx()
            y = chip.winfo_rooty() - top.winfo_rooty() + chip.winfo_height() + 4
            x = max(8, min(x, top.winfo_width() - width - 8))
            if y + height > top.winfo_height() - 4:        # no room below: go above
                y = max(4, chip.winfo_rooty() - top.winfo_rooty() - height - 4)
            card.place(x=x, y=y)
            card.lift()
            self._link_reveal = card
        except tk.TclError:
            self._link_reveal = None

    def _hide_link_reveal(self):
        after_id, self._reveal_after = getattr(self, "_reveal_after", None), None
        if after_id is not None:
            try:
                self.top.after_cancel(after_id)
            except tk.TclError:
                pass
        card, self._link_reveal = getattr(self, "_link_reveal", None), None
        if card is not None:
            try:
                card.destroy()
            except tk.TclError:
                pass

    def _expander(self, parent, row, text, flag):
        """A small click-to-reveal row ("▸ text" collapsed / "▾ text" open).
        ``flag`` names the bool attribute that holds its state, so the state
        survives the summary being rebuilt after an inline fix / re-check."""
        is_open = bool(getattr(self, flag))
        link = tk.Label(parent, text=("▾  " if is_open else "▸  ") + text, bg=COLORS["card"],
                        fg=COLORS["accent"], font=self.fonts.small_bold, cursor="hand2",
                        anchor="w")
        link.grid(row=row, column=0, sticky="w", pady=(0, 6))

        def _toggle(_event=None):
            setattr(self, flag, not is_open)
            self._render_summary()
            try:
                self.top.update_idletasks()
            except tk.TclError:
                pass
        link.bind("<Button-1>", _toggle)
        return link

    def _render_issues(self):
        """(Re)build the issues area: every must-fix card first, then every
        warning card, each with its inline fix button or a short hint. No banner,
        no "launch anyway" here, that lives only in the bottom bar. Called again
        after an inline fix or a re-check, so a resolved issue simply disappears."""
        for child in list(self.issues_frame.winfo_children()):
            child.destroy()
        self._issues = list(self.gather_issues())
        r = 0
        blocks = [i for i in self._issues if i.get("level") == "block"]
        warns = [i for i in self._issues if i.get("level") != "block"]
        # Must-fix at the very top: blocks disable Launch, warnings allow "Launch
        # anyway" (bottom bar). Order WITHIN each group is core's.
        for issue in blocks + warns:
            self._render_issue_row(r, issue)
            r += 1
        if self._fix_note is not None:
            ok, msg = self._fix_note
            tk.Label(self.issues_frame, text=("✓ " if ok else "") + msg,
                     bg=COLORS["card"], fg=COLORS["ok"] if ok else COLORS["error"],
                     font=self.fonts.small_bold, anchor="w", justify="left",
                     wraplength=450).grid(row=r, column=0, sticky="ew", pady=(2, 6))
            r += 1
        self._sync_issues_scroll()

    def _max_issues_height(self):
        """Tallest the issues area may grow before it scrolls: roughly three
        cards, less on a short screen so the summary + bottom bar stay in view."""
        try:
            screen = self.top.winfo_screenheight()
        except tk.TclError:
            screen = 800
        return max(150, min(340, screen - 520))

    def _sync_issues_scroll(self):
        """Size the issues canvas to its content; past the cap, fix the height
        and show the scrollbar. Few issues => full height, no scrollbar."""
        try:
            self.issues_frame.update_idletasks()
            has_rows = bool(self.issues_frame.winfo_children())
            need = self.issues_frame.winfo_reqheight() if has_rows else 0
            if not need:
                self._issues_scrollable = False
                self._issues_scroll.grid_remove()
                self._issues_outer.grid_remove()
                return
            self._issues_outer.grid()
            cap = self._max_issues_height()
            self._issues_scrollable = need > cap
            # The scrollbar takes its width FROM the canvas, so the dialog keeps
            # the same steady width whether or not the list scrolls.
            bar = (self._issues_scroll.winfo_reqwidth() + 4) if self._issues_scrollable else 0
            self._issues_canvas.configure(height=min(need, cap), width=self.WRAP + 20 - bar,
                                          scrollregion=(0, 0, self.WRAP + 20 - bar, need))
            if self._issues_scrollable:
                self._issues_scroll.grid(row=0, column=1, sticky="ns", padx=(4, 0))
            else:
                self._issues_scroll.grid_remove()
            self._issues_canvas.yview_moveto(0)
        except tk.TclError:
            pass

    def _scroll_issues(self, units):
        if self._issues_scrollable:
            try:
                self._issues_canvas.yview_scroll(units, "units")
            except tk.TclError:
                pass

    def _on_wheel(self, event):
        delta = getattr(event, "delta", 0)
        if not delta:
            return
        # Windows sends multiples of 120; macOS sends small raw deltas.
        step = -int(delta / 120) if abs(delta) >= 120 else (-1 if delta > 0 else 1)
        self._scroll_issues(step * 2)

    # How each issue level looks. Presentation only: the level itself (and so
    # whether Launch is allowed) is decided by core / gather_issues.
    _LEVEL_STYLE = {
        "block": {"fg": "error", "bg": "error_soft", "glyph": "■"},
        "warn": {"fg": "warn", "bg": "warn_soft", "glyph": "▲"},
    }

    def _fix_button(self, parent, text, command):
        """The inline one-click fix: a solid crimson button, unmistakably
        clickable next to the soft row background (hover darkens it)."""
        button = tk.Button(parent, text=text, command=command, font=self.fonts.small_bold,
                           bg=COLORS["accent"], fg="#ffffff",
                           activebackground=COLORS["accent_dark"], activeforeground="#ffffff",
                           relief="flat", bd=0, padx=12, pady=4, cursor="hand2")
        button.bind("<Enter>", lambda _e: button.configure(bg=COLORS["accent_dark"]))
        button.bind("<Leave>", lambda _e: button.configure(bg=COLORS["accent"]))
        return button

    def _render_issue_row(self, row, issue):
        level = "block" if issue.get("level") == "block" else "warn"
        style = self._LEVEL_STYLE[level]
        bg = COLORS[style["bg"]]
        fix = issue.get("fix")
        is_block_info = (issue.get("info") == "block")
        is_room_pick = (issue.get("kind") == "room_pick")
        # Row anatomy:  [colour rail] [glyph] [title / hint / secondary]  [fix button]
        frame = tk.Frame(self.issues_frame, bg=bg, highlightthickness=0)
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 5))
        frame.columnconfigure(2, weight=1)
        tk.Frame(frame, bg=COLORS[style["fg"]], width=4).grid(
            row=0, column=0, rowspan=4, sticky="ns")
        tk.Label(frame, text=style["glyph"], bg=bg, fg=COLORS[style["fg"]],
                 font=self.fonts.small_bold).grid(row=0, column=1, sticky="n",
                                                  padx=(9, 0), pady=(9, 0))
        # A fix button sits to the right of its issue, so reserve room for it
        # (and for the scrollbar a tall list gets).
        wrap = self.WRAP - (206 if fix else 66)
        tk.Label(frame, text=issue.get("title", ""), bg=bg, fg=COLORS["text"],
                 font=self.fonts.bold if level == "block" else self.fonts.body,
                 anchor="w", justify="left", wraplength=wrap).grid(
            row=0, column=2, sticky="ew", padx=(7, 10), pady=(7, 2))
        hint = str(issue.get("hint", "")).strip()
        if hint:
            tk.Label(frame, text=hint, bg=bg, fg=COLORS["muted"], font=self.fonts.small,
                     anchor="w", justify="left", wraplength=wrap).grid(
                row=1, column=2, sticky="ew", padx=(7, 10), pady=(0, 2))
        if fix:
            self._fix_button(frame, issue.get("fix_label", "Fix"),
                             lambda f=fix: self._run_fix(f)).grid(
                row=0, column=3, rowspan=2, sticky="e", padx=(0, 10), pady=8)
        if is_room_pick or (is_block_info and self.on_view_block):
            actions = tk.Frame(frame, bg=bg)
            actions.grid(row=2, column=2, columnspan=2, sticky="w", padx=(7, 10), pady=(2, 0))
            if is_room_pick:
                self._render_room_pick(actions, bg, issue)
            if is_block_info and self.on_view_block:
                link = tk.Label(actions, text="More information", bg=bg,
                                fg=COLORS["accent"], font=self.fonts.small_bold, cursor="hand2")
                link.pack(side="left")
                link.bind("<Button-1>", lambda _e: self.on_view_block())
        tk.Frame(frame, bg=bg, height=7).grid(row=3, column=2)

    def _render_room_pick(self, actions, bg, issue):
        """Inline "Rooms found" picker: choose one of the project's own rooms and
        apply it right here, so the room issue clears without leaving the screen."""
        rooms = [r for r in issue.get("rooms", []) if r]
        apply_room = issue.get("apply_room")
        if not rooms or apply_room is None:
            return
        tk.Label(actions, text="Rooms found:", bg=bg, fg=COLORS["muted"],
                 font=self.fonts.small).pack(side="left", padx=(0, 6))
        choice = tk.StringVar(actions, value=rooms[0])
        combo = ttk.Combobox(actions, textvariable=choice, state="readonly",
                             values=rooms, width=max(10, min(24, max(len(r) for r in rooms) + 2)))
        combo.pack(side="left", padx=(0, 6))

        def _use():
            self._run_fix(lambda: apply_room(choice.get()))
        self._fix_button(actions, "Use this room", _use).pack(side="left")

        # Addendum: define the chosen room in the project via the safe append
        # path, so a room the project does not have becomes valid at launch.
        add_room = issue.get("add_room")
        add_room_fix = issue.get("add_room_fix")
        if add_room and add_room_fix is not None:
            self._fix_button(actions, "Add %s room" % add_room,
                             lambda: self._run_fix(add_room_fix)).pack(side="left", padx=(6, 0))

    def _render_save(self):
        for child in list(self.save_frame.winfo_children()):
            child.destroy()
        if self._launching or not self.needs_save():
            return
        line = tk.Frame(self.save_frame, bg=COLORS["card"])
        line.grid(row=0, column=0, sticky="w", pady=(2, 4))
        tk.Label(line, text="These settings aren’t saved as a config yet.",
                 bg=COLORS["card"], fg=COLORS["muted"], font=self.fonts.small).pack(side="left")
        link = tk.Label(line, text="Save as a new config", bg=COLORS["card"],
                        fg=COLORS["accent"], font=self.fonts.small_bold, cursor="hand2")
        link.pack(side="left", padx=(6, 0))
        link.bind("<Button-1>", lambda _e: self._do_save())

    def _render_action(self):
        """The state-dependent bottom bar, the ONLY place a launch is offered:
          all clear     [Cancel]              ✓ Ready to launch   [Launch]
          warnings only [Cancel] [Re-check]   ( ⚠ N warnings  [Launch anyway] )
          must-fix      [Cancel] [Re-check]   Fix the must-fix item above…  [Launch] (disabled)
        """
        for child in list(self.action.winfo_children()):
            child.destroy()
        n_block = sum(1 for i in self._issues if i.get("level") == "block")
        n_warn = sum(1 for i in self._issues if i.get("level") == "warn")
        tk.Frame(self.action, bg=COLORS["card_line"], height=1).grid(
            row=0, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        left = tk.Frame(self.action, bg=COLORS["card"])
        left.grid(row=1, column=0, sticky="w")
        ttk.Button(left, text="Cancel", command=self._close).pack(side="left")
        if n_block or n_warn:
            # Something fixed outside the launcher (database started, port freed)?
            ttk.Button(left, text="Re-check", style="Slim.TButton",
                       command=self._recheck).pack(side="left", padx=(8, 0))
        if n_block:
            # A hard block stands: launching is disabled until it is fixed.
            tk.Label(self.action, text="Fix the %s above to launch."
                     % ("must-fix item" if n_block == 1 else "%d must-fix items" % n_block),
                     bg=COLORS["card"], fg=COLORS["error"], font=self.fonts.small,
                     anchor="e").grid(row=1, column=1, sticky="e", padx=(0, 10))
            tk.Button(self.action, text="Launch", state="disabled", relief="flat", bd=0,
                      font=self.fonts.bold, bg=COLORS["field_off"], fg=COLORS["faint"],
                      padx=16, pady=6, disabledforeground=COLORS["faint"],
                      highlightthickness=1, highlightbackground=COLORS["card_line"],
                      cursor="arrow").grid(row=1, column=2, sticky="e")
            self.top.bind("<Return>", lambda _e: None)
            return
        if n_warn:
            # The single highlighted callout: icon + "N warnings" + Launch anyway.
            callout = tk.Frame(self.action, bg=COLORS["warn_soft"], highlightthickness=1,
                               highlightbackground=COLORS["warn"])
            callout.grid(row=1, column=1, columnspan=2, sticky="e")
            tk.Label(callout, text="⚠", bg=COLORS["warn_soft"], fg=COLORS["warn"],
                     font=self.fonts.bold).pack(side="left", padx=(12, 0), pady=7)
            tk.Label(callout, text="%d warning%s" % (n_warn, "" if n_warn == 1 else "s"),
                     bg=COLORS["warn_soft"], fg=COLORS["warn"],
                     font=self.fonts.bold).pack(side="left", padx=(6, 12), pady=7)
            okay = tk.Button(callout, text="Launch anyway", command=self._okay,
                             font=self.fonts.bold, bg=COLORS["warn"], fg="#ffffff",
                             activebackground=COLORS["warn"], activeforeground="#ffffff",
                             relief="flat", bd=0, padx=16, pady=6, cursor="hand2")
            okay.pack(side="left", padx=(0, 7), pady=7)
        else:
            ready = tk.Frame(self.action, bg=COLORS["card"])
            ready.grid(row=1, column=1, sticky="e", padx=(0, 12))
            tk.Label(ready, text="✓", bg=COLORS["card"], fg=COLORS["ok"],
                     font=self.fonts.bold).pack(side="left")
            tk.Label(ready, text="Ready to launch", bg=COLORS["card"], fg=COLORS["ok"],
                     font=self.fonts.bold).pack(side="left", padx=(5, 0))
            okay = tk.Button(self.action, text="Launch", command=self._okay,
                             font=self.fonts.bold, bg=COLORS["accent"], fg="#ffffff",
                             activebackground=COLORS["accent_dark"], activeforeground="#ffffff",
                             relief="flat", bd=0, padx=20, pady=6, cursor="hand2")
            okay.grid(row=1, column=2, sticky="e")
        self.launch_button = okay
        self.top.bind("<Return>", lambda _e: self._okay())
        try:
            okay.focus_set()
        except tk.TclError:
            pass

    def _run_fix(self, fix):
        try:
            result = fix()
        except Exception as error:  # a fix must never crash the dialog
            result = (False, str(error))
        if result is None:
            # Nothing happened (e.g. the folder picker was cancelled): no note.
            self._fix_note = None
        else:
            ok, msg = result
            self._fix_note = (bool(ok), str(msg))
        self._rerender()

    def _recheck(self):
        self._fix_note = None
        self._rerender()

    def _do_save(self):
        if self.on_save is not None:
            self.on_save()
        self._rerender()

    def _rerender(self):
        self._render_summary()
        self._render_issues()
        self._render_save()
        self._render_action()
        try:
            self.top.update_idletasks()
            _center_on(self.parent, self.top)
        except tk.TclError:
            pass

    def _copy_link(self, text):
        """Copy the per-seat link. Feedback goes on the Copy link button when the
        link is on screen (non-study room), else on the room chip it hides behind."""
        try:
            self.top.clipboard_clear()
            self.top.clipboard_append(text)
        except tk.TclError:
            return
        button, chip = self.copy_link_button, self.room_chip
        self._hide_link_reveal()

        def _flash(widget, during, after):
            def _restore():
                try:
                    if widget.winfo_exists():
                        widget.configure(text=after)
                except tk.TclError:
                    pass
            try:
                widget.configure(text=during)
                self.top.after(1200, _restore)
            except tk.TclError:
                pass
        if button is not None:
            _flash(button, "Copied", "Copy link")
        elif chip is not None:
            _flash(chip, "Link copied", str(chip.cget("text")))

    def _okay(self):
        # Start the launch, then transition THIS popup into a neutral "launching"
        # state. It does NOT claim success yet: the real success/failure banner is
        # set later by set_result, driven by the launch worker's actual outcome.
        if self._launching:
            return
        # Safety net: never launch while a hard block still stands (the button is
        # disabled in that state, but Return could otherwise reach here).
        if any(i.get("level") == "block" for i in self._issues):
            return
        self._launching = True
        self.on_okay()
        self._enter_launching()

    def _enter_launching(self):
        try:
            self.top.title("Launching…")
        except tk.TclError:
            pass
        # Clear the issues + save affordance so the popup becomes a clean handoff.
        for frame in (self.issues_frame, self.save_frame):
            for child in list(frame.winfo_children()):
                child.destroy()
        self._sync_issues_scroll()
        for child in list(self.action.winfo_children()):
            child.destroy()
        self._handoff_line = tk.Frame(self.action, bg=COLORS["card"])
        self._handoff_line.grid(row=0, column=0, sticky="w")
        tk.Label(self._handoff_line,
                 text="Starting the server… this window will confirm when it is up.",
                 bg=COLORS["card"], fg=COLORS["muted"], font=self.fonts.small).pack(side="left")
        # While launching, closing/Enter/Escape just dismiss this popup; the
        # launch keeps running and reports to the activity log regardless.
        self.top.bind("<Return>", lambda _e: self._close())
        self.top.bind("<Escape>", lambda _e: self._close())
        self.top.protocol("WM_DELETE_WINDOW", self._close)
        try:
            self.top.update_idletasks()
            _center_on(self.parent, self.top)
        except tk.TclError:
            pass

    def set_result(self, ok, info):
        """Show the REAL outcome. On success ``info`` is the open-dashboard result
        dict (``method`` cookie/manual) and the banner reports HONESTLY whether the
        dashboard opened already logged in or at the login page, with a working
        re-open link; on failure ``info`` is a reason string and the banner shows a
        clear failure state, never a false 'dashboard opening'.
        """
        line = getattr(self, "_handoff_line", None)
        if line is None:
            return
        try:
            for child in list(line.winfo_children()):
                child.destroy()
        except tk.TclError:
            return
        if ok:
            method = info.get("method") if isinstance(info, dict) else None
            if isinstance(info, dict) and info.get("monitor_url"):
                self.takeover_url = info["monitor_url"]
            elif info and not isinstance(info, dict):
                self.takeover_url = info
            try:
                self.top.title("Session running")
            except tk.TclError:
                pass
            if method == "cookie":
                lead = "Experiment launched. The dashboard opened already logged in. No dashboard? "
            elif method == "manual":
                lead = ("Experiment launched. The dashboard opened at the login page; log in with "
                        "the username and password above. Not opened? ")
            else:
                lead = "Experiment launched, dashboard opening. No dashboard? "
            tk.Label(line, text=lead, bg=COLORS["card"], fg=COLORS["muted"],
                     font=self.fonts.small, anchor="w", justify="left",
                     wraplength=440).pack(side="left")
            link = tk.Label(line, text="Click here", bg=COLORS["card"], fg=COLORS["accent"],
                            font=self.fonts.small_bold, cursor="hand2")
            link.pack(side="left")
            link.bind("<Button-1>", lambda _e: self._open_takeover_url())
        else:
            try:
                self.top.title("Launch failed")
            except tk.TclError:
                pass
            tk.Label(line, text="Launch failed: see the activity log.",
                     bg=COLORS["card"], fg=COLORS["error"],
                     font=self.fonts.small_bold, anchor="w", justify="left").pack(side="left")
            if info:
                tk.Label(self.action, text=str(info), bg=COLORS["card"], fg=COLORS["muted"],
                         font=self.fonts.small, anchor="w", justify="left",
                         wraplength=460).grid(row=1, column=0, sticky="w", pady=(4, 0))

    def _open_takeover_url(self):
        # Prefer the real authenticated re-open (fresh form-login + cookie relay);
        # fall back to opening the plain monitor URL in the default browser.
        if self.on_open_dashboard is not None:
            self.on_open_dashboard()
        elif self.takeover_url:
            webbrowser.open(self.takeover_url)

    def _close(self):
        _modal_close(self.top)


def _copyable_line(parent, fonts, text):
    """A monospace, selectable one-line box (so a link can be copied)."""
    box = tk.Frame(parent, bg=COLORS["field_off"], highlightthickness=1,
                   highlightbackground=COLORS["card_line"])
    box.columnconfigure(0, weight=1)
    entry = tk.Entry(box, font=fonts.mono_small, bg=COLORS["field_off"],
                     fg=COLORS["text"], relief="flat", readonlybackground=COLORS["field_off"])
    entry.insert(0, text)
    entry.configure(state="readonly")
    entry.grid(row=0, column=0, sticky="ew", padx=6, pady=5)
    return box


class DatabasePickerDialog(object):
    """Pick the database for this config. Lists the oTree default (SQLite), the
    lab shared Postgres and EVERY custom database in the global registry, each
    custom line showing its creator researcher in grey on the same line, plus an
    "Add new database…" action. The list is core.list_databases, so this picker
    and the web one show the same databases; selecting one applies it to the
    config through app.choose_database. The list is global on purpose (anybody
    may use anybody else's database; the grey name is the only "whose is it")."""

    def __init__(self, parent, fonts, app):
        self.app = app
        self.fonts = fonts
        top = self.top = tk.Toplevel(parent)
        top.title("Choose a database")
        top.configure(bg=COLORS["card"])
        _attach_shade(parent, top)
        top.transient(parent)
        top.resizable(False, False)

        body = tk.Frame(top, bg=COLORS["card"])
        body.pack(fill="both", expand=True, padx=18, pady=16)
        body.columnconfigure(0, weight=1)

        tk.Label(body, text="Choose a database", bg=COLORS["card"], fg=COLORS["text"],
                 font=fonts.bold, anchor="w").pack(fill="x")
        tk.Label(body,
                 text="Everyone shares this list. A grey name shows who created that database.",
                 bg=COLORS["card"], fg=COLORS["muted"], font=fonts.small, anchor="w",
                 justify="left", wraplength=440).pack(fill="x", pady=(2, 10))

        current = app.current_database_id()
        for entry in core.list_databases(app.store_extra):
            self._row(body, entry, selected=(entry["id"] == current))

        tk.Frame(body, bg=COLORS["card_line"], height=1).pack(fill="x", pady=(10, 8))
        add = tk.Label(body, text="+  Add new database…", bg=COLORS["card"],
                       fg=COLORS["accent"], font=fonts.small_bold, anchor="w", cursor="hand2")
        add.pack(fill="x")
        add.bind("<Button-1>", lambda _e: self._add_new())

        buttons = tk.Frame(body, bg=COLORS["card"])
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="Cancel", command=self._close).pack(side="right")

        _center_on(parent, top)
        top.bind("<Escape>", lambda _e: self._close())
        try:
            _grab_modal(top)
        except tk.TclError:
            pass

    def _row(self, parent, entry, selected=False):
        row = tk.Frame(parent, bg=COLORS["card"], cursor="hand2")
        row.pack(fill="x", pady=2)
        tk.Label(row, text=("●" if selected else " "), bg=COLORS["card"],
                 fg=COLORS["accent"] if selected else COLORS["card"],
                 font=self.fonts.small_bold, width=2).pack(side="left")
        tk.Label(row, text=entry["title"], bg=COLORS["card"], fg=COLORS["text"],
                 font=self.fonts.small_bold if selected else self.fonts.body,
                 anchor="w").pack(side="left")
        if entry.get("researcher"):
            # The creator researcher, in grey, on the SAME line (whose DB is this).
            tk.Label(row, text="   created by %s" % entry["researcher"], bg=COLORS["card"],
                     fg=COLORS["faint"], font=self.fonts.small, anchor="w").pack(side="left")
        row.bind("<Button-1>", lambda _e, e=entry: self._choose(e))
        for child in row.winfo_children():
            child.bind("<Button-1>", lambda _e, e=entry: self._choose(e))

    def _choose(self, entry):
        self.app.choose_database(entry)
        self._close()

    def _add_new(self):
        self._close()
        self.app.create_database_dialog()

    def _close(self):
        _modal_close(self.top)


class CreateDatabaseDialog(object):
    """Collect a new database name (and optional user/password), then create it
    through core.create_database using the Lab Settings admin config. On a
    confirmed create the app auto-fills the Custom database fields; on anything
    else nothing changes and the real error is shown, selectable to copy."""

    def __init__(self, parent, fonts, app, suggested_name="",
                 researchers=None, suggested_researcher=""):
        self.app = app
        self.fonts = fonts
        self._created = False
        top = self.top = tk.Toplevel(parent)
        top.title("Create a new database")
        top.configure(bg=COLORS["card"])
        _attach_shade(parent, top)
        top.transient(parent)
        top.resizable(False, False)

        body = tk.Frame(top, bg=COLORS["card"])
        body.pack(fill="both", expand=True, padx=20, pady=18)
        body.columnconfigure(1, weight=1)

        tk.Label(body, text="Create a new Postgres database",
                 bg=COLORS["card"], fg=COLORS["text"], font=fonts.bold,
                 anchor="w").grid(row=0, column=0, columnspan=2, sticky="ew")
        tk.Label(body,
                 text="Uses the admin details from Lab Settings. The new user and password are "
                      "optional; leave them blank to let the admin role own the database.",
                 bg=COLORS["card"], fg=COLORS["faint"], font=fonts.small, anchor="w",
                 justify="left", wraplength=420).grid(row=1, column=0, columnspan=2,
                                                      sticky="ew", pady=(2, 12))

        self.name = tk.StringVar(top, value=suggested_name)   # prefilled from the project folder
        self.user = tk.StringVar(top)
        self.password = tk.StringVar(top)
        self._row(body, 2, "Database name", ttk.Entry(body, textvariable=self.name))
        # Researcher (required): whose database this is. Type a new name or pick
        # from the shared roster (the same list Save-as-new uses). Recorded in the
        # registry so every config's picker shows the creator.
        self.researcher = tk.StringVar(top, value=suggested_researcher)
        self.researcher_combo = ttk.Combobox(
            body, textvariable=self.researcher, values=list(researchers or []))
        self._row(body, 3, "Researcher (required)", self.researcher_combo)
        self._row(body, 4, "New user (optional)", ttk.Entry(body, textvariable=self.user))
        # Password entry with the Show toggle inline to its right (aligned like
        # the main window's password rows).
        pwframe = tk.Frame(body, bg=COLORS["card"])
        pwframe.columnconfigure(0, weight=1)
        self.pw_entry = ttk.Entry(pwframe, textvariable=self.password, show=MASK_CHAR)
        self.pw_entry.grid(row=0, column=0, sticky="ew")
        self.show_pw = tk.BooleanVar(top, value=False)
        ttk.Checkbutton(pwframe, text="Show", variable=self.show_pw,
                        command=self._toggle_pw).grid(row=0, column=1, padx=(6, 0))
        self._row(body, 5, "New password (optional)", pwframe)

        self.status = tk.Label(body, text="", bg=COLORS["card"], fg=COLORS["muted"],
                               font=fonts.small, anchor="w", justify="left", wraplength=420)
        self.status.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        # A selectable copy of the last message, so an error can be copied out.
        self.detail = tk.Text(body, height=3, font=fonts.mono_small, wrap="word",
                              bg=COLORS["field_off"], fg=COLORS["text"], relief="flat",
                              highlightthickness=1, highlightbackground=COLORS["card_line"])
        self.detail.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.detail.configure(state="disabled")
        self.detail.grid_remove()

        buttons = tk.Frame(body, bg=COLORS["card"])
        buttons.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        buttons.columnconfigure(0, weight=1)
        # "Cancel" until a create succeeds, then "Close".
        self.cancel_button = ttk.Button(buttons, text="Cancel", command=self._close)
        self.cancel_button.grid(row=0, column=0, sticky="w")
        self.create_button = tk.Button(
            buttons, text="Create", command=self._create, font=fonts.bold,
            bg=COLORS["accent"], fg="#ffffff", activebackground=COLORS["accent_dark"],
            activeforeground="#ffffff", relief="flat", padx=14, pady=6, cursor="hand2")
        self.create_button.grid(row=0, column=1, sticky="e")

        _center_on(parent, top)
        try:
            _grab_modal(top)
        except tk.TclError:
            pass

    def _row(self, body, r, label, widget):
        tk.Label(body, text=label, bg=COLORS["card"], fg=COLORS["muted"],
                 font=self.fonts.body, anchor="w").grid(row=r, column=0, sticky="w", pady=3, padx=(0, 10))
        widget.grid(row=r, column=1, sticky="ew", pady=3)

    def _toggle_pw(self):
        self.pw_entry.configure(show="" if self.show_pw.get() else MASK_CHAR)

    def _create(self):
        name = self.name.get().strip()
        if not name:
            self._set_status("Enter a database name.", COLORS["warn"])
            return
        researcher = self.researcher.get().strip()
        if not researcher:
            self._set_status("Enter or pick a researcher: whose database this is.",
                             COLORS["warn"])
            return
        self.create_button.configure(state="disabled", text="Creating...")
        self._set_status("Creating the database...", COLORS["muted"])
        thread = threading.Thread(
            target=self.app._run_create_database,
            args=(name, self.user.get().strip(), self.password.get(), researcher, self._done),
            daemon=True)
        thread.start()

    def _done(self, result):
        self.create_button.configure(state="normal", text="Create")
        if result.get("ok"):
            self._created = True
            self.cancel_button.configure(text="Close")
            self._set_status(result.get("message", "Created."), COLORS["ok"])
            self.app.apply_created_database(result.get("fields", {}))
            self.app.log(result.get("message", "Database created."), "ok")
            self.top.after(500, self._close_and_offer)
        else:
            self._set_status(result.get("message", "Could not create the database."),
                             COLORS["error"])

    def _close_and_offer(self):
        self._close()
        # Offer to keep the new (private) database as a saved config.
        self.app.offer_save_after_create()

    def _set_status(self, text, color):
        self.status.configure(text=text, fg=color)
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)
        self.detail.configure(state="disabled")
        self.detail.grid()

    def _close(self):
        _modal_close(self.top)


class RoomPickerDialog(object):
    """Pick the room to serve. Default stays study. 'Read this project's rooms'
    imports the project's own settings in a throwaway subprocess (core) and
    lists them; on empty or failure it falls back to a free-text box and shows
    the real reason. Selecting a room does not change the lab or the seats.

    Rooms the lab PC desktop shortcuts already open (core.room_has_participant
    _links, currently just the study room) are marked "with participant PC
    links" and highlighted, and a small info button explains what that means.
    """

    def __init__(self, parent, fonts, app):
        self.app = app
        self.fonts = fonts
        self._room_names = []   # real room names, parallel to the listbox rows
        top = self.top = tk.Toplevel(parent)
        top.title("Choose the room")
        top.configure(bg=COLORS["card"])
        _attach_shade(parent, top)
        top.transient(parent)
        top.resizable(False, False)

        body = tk.Frame(top, bg=COLORS["card"])
        body.pack(fill="both", expand=True, padx=20, pady=18)
        body.columnconfigure(0, weight=1)

        # Heading + a small info ("i") button that explains participant PC links.
        header = tk.Frame(body, bg=COLORS["card"])
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        tk.Label(header, text="Choose the room to serve", bg=COLORS["card"], fg=COLORS["text"],
                 font=fonts.bold, anchor="w").grid(row=0, column=0, sticky="w")
        ttk.Button(header, text="i", width=3, style="Slim.TButton",
                   command=self._toggle_info).grid(row=0, column=1, sticky="e")

        tk.Label(body,
                 text="The study room is the lab default: the lab computers' desktop shortcuts "
                      "open its participant PC links. Only change it if the computers will open a "
                      "different room's link.",
                 bg=COLORS["card"], fg=COLORS["faint"], font=fonts.small, anchor="w",
                 justify="left", wraplength=460).grid(row=1, column=0, sticky="ew", pady=(2, 8))

        # Info panel, hidden until the "i" button is pressed.
        self.info = tk.Frame(body, bg=COLORS["accent_soft"], highlightthickness=1,
                             highlightbackground=COLORS["accent"])
        tk.Label(self.info, bg=COLORS["accent_soft"], fg=COLORS["text"], font=fonts.small,
                 anchor="w", justify="left", wraplength=440,
                 text=("Rooms marked \"with participant PC links\" are the ones the lab computers "
                       "are already set up for: their desktop shortcuts open "
                       "http://HOST:8000/room/ROOM?participant_label=SEAT, so participants can "
                       "join straight from the lab PCs. Right now that is the %r room, "
                       "highlighted in the list below.\n\n"
                       "If you pick a room WITHOUT participant PC links, the desktop shortcuts do "
                       "not point at it, so the lab PCs will not open it on their own. You would "
                       "then have to open the correct /room/YOURROOM?participant_label=SEAT link "
                       "on each computer yourself." % DEFAULT_ROOM_NAME)
                 ).pack(fill="x", padx=10, pady=8)
        self.info.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self.info.grid_remove()

        # The lab-default room is the primary, recommended choice: a larger
        # accent button so a user naturally picks it. The "with participant PC
        # links" wording lives ONLY here on the button, so it is clear why you
        # press it; the list row below just reads "study (lab default)".
        tk.Button(body, text="Use %s (lab default) with participant PC links" % DEFAULT_ROOM_NAME,
                  command=lambda: self._choose(DEFAULT_ROOM_NAME),
                  font=fonts.bold, bg=COLORS["accent"], fg="#ffffff",
                  activebackground=COLORS["accent_dark"], activeforeground="#ffffff",
                  relief="flat", padx=14, pady=10, cursor="hand2").grid(
            row=3, column=0, sticky="ew", pady=(0, 8))

        # The project's own rooms load automatically in the background and appear
        # below, with study pinned at the top and highlighted.
        self.note = tk.Label(body, text="", bg=COLORS["card"], fg=COLORS["muted"],
                             font=fonts.small, anchor="w", justify="left", wraplength=460)
        self.note.grid(row=5, column=0, sticky="ew", pady=(8, 0))

        self.listbox = tk.Listbox(body, height=6, font=fonts.body,
                                  highlightthickness=1, highlightbackground=COLORS["card_line"])
        self.listbox.grid(row=6, column=0, sticky="ew", pady=(6, 0))
        self.listbox.grid_remove()
        self.listbox.bind("<Double-Button-1>", lambda _e: self._pick_from_list())
        self.listbox.bind("<Return>", lambda _e: self._pick_from_list())
        self.pick_button = ttk.Button(body, text="Use the selected room",
                                      command=self._pick_from_list)
        self.pick_button.grid(row=7, column=0, sticky="ew", pady=(6, 0))
        self.pick_button.grid_remove()

        free = tk.Frame(body, bg=COLORS["card"])
        free.grid(row=8, column=0, sticky="ew", pady=(10, 0))
        free.columnconfigure(0, weight=1)
        self.free = tk.StringVar(top, value="")
        tk.Label(free, text="Or type a room name:", bg=COLORS["card"], fg=COLORS["muted"],
                 font=fonts.small, anchor="w").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Entry(free, textvariable=self.free).grid(row=1, column=0, sticky="ew", pady=(2, 0))
        ttk.Button(free, text="Use this name",
                   command=self._use_free).grid(row=1, column=1, sticky="e", padx=(6, 0))

        ttk.Button(body, text="Cancel", command=self._close).grid(row=9, column=0, sticky="w",
                                                                  pady=(12, 0))
        _center_on(parent, top)
        try:
            _grab_modal(top)
        except tk.TclError:
            pass
        self._start_enumerate()

    def _room_display(self, name):
        """The room name with its muted "(lab default)" tag; the study room reads
        "study (lab default)". The "with participant PC links" wording lives only
        on the primary button, not in the list."""
        label = name
        if name == DEFAULT_ROOM_NAME:
            label += " (lab default)"
        return label

    def _toggle_info(self):
        if self.info.winfo_ismapped():
            self.info.grid_remove()
        else:
            self.info.grid()

    def _start_enumerate(self):
        """Read the project's rooms off the UI thread (core has an 8s timeout).

        The worker only stores the result; the main thread polls for it with
        after(), so no Tk call is ever made from the worker thread.
        """
        self.note.configure(text="Reading this project's rooms…", fg=COLORS["muted"])
        project = self.app.var["project_path"].get()
        self._enum_result = None

        def work():
            self._enum_result = self.app.enumerate_rooms(project)

        threading.Thread(target=work, name="room-enum", daemon=True).start()
        self._poll_rooms()

    def _poll_rooms(self):
        if self._enum_result is not None:
            self._on_rooms(self._enum_result)
            return
        try:
            self.top.after(100, self._poll_rooms)
        except tk.TclError:
            pass

    def _on_rooms(self, result):
        """Populate the list: study pinned + highlighted at the top (the lab
        default, with participant PC links), then this project's other rooms."""
        self.listbox.delete(0, "end")
        self._room_names = [DEFAULT_ROOM_NAME]
        self.listbox.insert("end", self._room_display(DEFAULT_ROOM_NAME))
        self.listbox.itemconfig(0, foreground=COLORS["accent"], selectforeground=COLORS["accent"])
        if result.get("ok"):
            for name in result.get("rooms", []):
                if name == DEFAULT_ROOM_NAME:
                    continue
                self._room_names.append(name)
                idx = len(self._room_names) - 1
                self.listbox.insert("end", self._room_display(name))
                if core.room_has_participant_links(name):
                    self.listbox.itemconfig(idx, foreground=COLORS["accent"],
                                            selectforeground=COLORS["accent"])
            if result.get("empty"):
                self.note.configure(
                    text="This project defines no rooms of its own. Study is the lab default.",
                    fg=COLORS["muted"])
            else:
                self.note.configure(text="", fg=COLORS["muted"])
        else:
            self.note.configure(
                text="Could not read this project's rooms (%s). Study is the lab default, "
                     "or type a name below." % result.get("error", ""), fg=COLORS["warn"])
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(0)     # study preselected
        self.listbox.grid()
        self.pick_button.grid()

    def _pick_from_list(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        self._choose(self._room_names[sel[0]])

    def _use_free(self):
        name = self.free.get().strip()
        if name:
            self._choose(name)

    def _choose(self, name):
        self.app.set_room(name)
        self._close()

    def _close(self):
        _modal_close(self.top)


class LabPresetEditDialog(object):
    """Add or edit one lab preset (name, IP, seat list). Validation and the
    write both go through core; the caller passes on_save(name, ip, seats)."""

    def __init__(self, parent, fonts, title, preset, on_save):
        self.on_save = on_save
        top = self.top = tk.Toplevel(parent)
        top.title(title)
        top.configure(bg=COLORS["card"])
        _attach_shade(parent, top)
        top.transient(parent)
        top.resizable(False, False)

        body = tk.Frame(top, bg=COLORS["card"])
        body.pack(fill="both", expand=True, padx=20, pady=18)
        body.columnconfigure(0, weight=1)

        self.name = tk.StringVar(top, value=(preset or {}).get("name", ""))
        self.ip = tk.StringVar(top, value=(preset or {}).get("ip", ""))
        tk.Label(body, text="Lab name", bg=COLORS["card"], fg=COLORS["muted"],
                 font=fonts.body, anchor="w").grid(row=0, column=0, sticky="w")
        ttk.Entry(body, textvariable=self.name).grid(row=1, column=0, sticky="ew", pady=(2, 8))
        tk.Label(body, text="IP address or host", bg=COLORS["card"], fg=COLORS["muted"],
                 font=fonts.body, anchor="w").grid(row=2, column=0, sticky="w")
        ttk.Entry(body, textvariable=self.ip).grid(row=3, column=0, sticky="ew", pady=(2, 8))
        tk.Label(body, text="Seat labels (one per line, or spaces/commas)",
                 bg=COLORS["card"], fg=COLORS["muted"], font=fonts.body,
                 anchor="w").grid(row=4, column=0, sticky="w")
        self.seats = tk.Text(body, height=6, font=fonts.mono_small, wrap="word",
                             highlightthickness=1, highlightbackground=COLORS["card_line"])
        self.seats.grid(row=5, column=0, sticky="ew", pady=(2, 0))
        self.seats.insert("1.0", " ".join((preset or {}).get("seats", [])))

        # Optional column count so the plain-grid seat map matches the room shape.
        colrow = tk.Frame(body, bg=COLORS["card"])
        colrow.grid(row=6, column=0, sticky="w", pady=(8, 0))
        tk.Label(colrow, text="Columns in the room grid (optional)", bg=COLORS["card"],
                 fg=COLORS["muted"], font=fonts.body).pack(side="left", padx=(0, 8))
        self.cols = tk.StringVar(top, value=str((preset or {}).get("cols", "") or ""))
        ttk.Entry(colrow, textvariable=self.cols, width=5).pack(side="left")
        tk.Label(colrow, text="blank = auto", bg=COLORS["card"], fg=COLORS["faint"],
                 font=fonts.small).pack(side="left", padx=(8, 0))

        self.status = tk.Label(body, text="", bg=COLORS["card"], fg=COLORS["error"],
                               font=fonts.small, anchor="w", justify="left", wraplength=420)
        self.status.grid(row=7, column=0, sticky="ew", pady=(6, 0))

        buttons = tk.Frame(body, bg=COLORS["card"])
        buttons.grid(row=8, column=0, sticky="ew", pady=(12, 0))
        buttons.columnconfigure(0, weight=1)
        ttk.Button(buttons, text="Cancel", command=self._close).grid(row=0, column=0, sticky="w")
        tk.Button(buttons, text="Save", command=self._save, font=fonts.bold,
                  bg=COLORS["accent"], fg="#ffffff", activebackground=COLORS["accent_dark"],
                  activeforeground="#ffffff", relief="flat", padx=14, pady=5,
                  cursor="hand2").grid(row=0, column=1, sticky="e")
        _center_on(parent, top)
        try:
            _grab_modal(top)
        except tk.TclError:
            pass

    def _save(self):
        raw = self.cols.get().strip()
        try:
            cols = max(0, int(raw)) if raw else 0
        except ValueError:
            self.status.configure(text="Columns must be a whole number, or blank for auto.")
            return
        ok, message = self.on_save(self.name.get(), self.ip.get(),
                                   self.seats.get("1.0", "end"), cols)
        if ok:
            self._close()
        else:
            self.status.configure(text=message)

    def _close(self):
        _modal_close(self.top)


class LabSettingsDialog(object):
    """The Lab Settings page: the Postgres admin config (used only to create
    databases) and the lab presets that drive the main lab selector. Every
    change is saved to presets.json through the app; preset add/edit/delete and
    show/hide all go through core so the guards (no clobber, keep a visible lab)
    are the single source of truth."""

    def __init__(self, parent, fonts, app):
        self.app = app
        self.fonts = fonts
        top = self.top = tk.Toplevel(parent)
        top.title("Lab Settings")
        top.configure(bg=COLORS["window"])
        _attach_shade(parent, top)
        top.transient(parent)
        top.geometry("640x720")
        top.minsize(560, 560)

        # Scrollable so the added Database section (admin + custom list + default)
        # can never push the lab presets off a short screen.
        scroll = ScrollFrame(top, COLORS["window"])
        scroll.pack(fill="both", expand=True)
        outer = tk.Frame(scroll.inner, bg=COLORS["window"])
        outer.pack(fill="both", expand=True, padx=16, pady=14)
        outer.columnconfigure(0, weight=1)

        # -- Which lab is this computer (item 8): the UI-settable machine identity.
        self._build_identity_card(outer)

        # -- Postgres admin config --------------------------------------------
        admin_card = tk.Frame(outer, bg=COLORS["card"], highlightthickness=1,
                              highlightbackground=COLORS["card_line"])
        admin_card.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        admin_card.columnconfigure(1, weight=1)
        tk.Label(admin_card, text="Postgres admin (used only to create databases)",
                 bg=COLORS["card"], fg=COLORS["text"], font=fonts.bold, anchor="w").grid(
            row=0, column=0, columnspan=3, sticky="ew", padx=12, pady=(10, 2))
        tk.Label(admin_card,
                 text="Only used by “Create a new database”. Never used to launch.",
                 bg=COLORS["card"], fg=COLORS["faint"], font=fonts.small, anchor="w",
                 justify="left", wraplength=560).grid(row=1, column=0, columnspan=3,
                                                      sticky="ew", padx=12, pady=(0, 8))
        admin = core.pg_admin_from_store(app.store_extra)
        self.a_user = tk.StringVar(top, value=admin["admin_username"])
        self.a_pw = tk.StringVar(top, value=admin["admin_password"])
        self.a_host = tk.StringVar(top, value=admin["admin_host"])
        self.a_port = tk.StringVar(top, value=admin["admin_port"])
        a_user_entry = ttk.Entry(admin_card, textvariable=self.a_user)
        self._admin_row(admin_card, 2, "Admin username", a_user_entry)
        self.a_pw_entry = ttk.Entry(admin_card, textvariable=self.a_pw, show=MASK_CHAR)
        self._admin_row(admin_card, 3, "Admin password", self.a_pw_entry)
        self.show_pw = tk.BooleanVar(top, value=False)
        ttk.Checkbutton(admin_card, text="Show", variable=self.show_pw,
                        command=self._toggle_admin_pw).grid(row=3, column=2, sticky="w", padx=(4, 12))
        a_host_entry = ttk.Entry(admin_card, textvariable=self.a_host)
        self._admin_row(admin_card, 4, "Host", a_host_entry)
        a_port_entry = ttk.Entry(admin_card, textvariable=self.a_port)
        self._admin_row(admin_card, 5, "Port", a_port_entry)
        # The admin details save on their own (no button to forget): each field
        # saves when it loses focus, and again when the dialog closes.
        for _e in (a_user_entry, self.a_pw_entry, a_host_entry, a_port_entry):
            _e.bind("<FocusOut>", lambda _ev: self._save_admin())
        arow = tk.Frame(admin_card, bg=COLORS["card"])
        arow.grid(row=6, column=0, columnspan=3, sticky="ew", padx=12, pady=(6, 10))
        arow.columnconfigure(0, weight=1)
        self.admin_status = tk.Label(arow, text="Saved automatically.", bg=COLORS["card"],
                                     fg=COLORS["faint"], font=fonts.small, anchor="w")
        self.admin_status.grid(row=0, column=0, sticky="w")

        # -- Database section: custom databases (the global registry) ---------
        self._build_custom_db_card(outer, row=2)
        # -- Database section: default database (oTree default / lab shared) ---
        self._build_default_db_card(outer, row=3)

        # -- Lab presets ------------------------------------------------------
        presets_card = tk.Frame(outer, bg=COLORS["card"], highlightthickness=1,
                               highlightbackground=COLORS["card_line"])
        presets_card.grid(row=4, column=0, sticky="nsew", pady=(12, 0))
        presets_card.columnconfigure(0, weight=1)
        presets_card.rowconfigure(2, weight=1)
        tk.Label(presets_card, text="Lab presets", bg=COLORS["card"], fg=COLORS["text"],
                 font=fonts.bold, anchor="w").grid(row=0, column=0, sticky="ew",
                                                   padx=12, pady=(10, 2))
        tk.Label(presets_card,
                 text="Shown labs appear in the main lab selector. When only one lab is shown it "
                      "is auto-selected. Small lab and Large lab are seeded from the lab constants.",
                 bg=COLORS["card"], fg=COLORS["faint"], font=fonts.small, anchor="w",
                 justify="left", wraplength=560).grid(row=1, column=0, sticky="ew",
                                                      padx=12, pady=(0, 8))
        # Give rows enough height for the font (they clipped at the default).
        try:
            ttk.Style(top).configure(
                "Treeview", rowheight=int(self.fonts.body.metrics("linespace")) + 8)
        except tk.TclError:
            pass
        self.tree = ttk.Treeview(presets_card, columns=("ip", "seats", "shown"),
                                 show="tree headings", height=6)
        self.tree.heading("#0", text="Lab")
        self.tree.heading("ip", text="IP")
        self.tree.heading("seats", text="Seats")
        self.tree.heading("shown", text="Shown")
        self.tree.column("#0", width=190)
        self.tree.column("ip", width=140)
        self.tree.column("seats", width=60, anchor="center")
        self.tree.column("shown", width=70, anchor="center")
        self.tree.grid(row=2, column=0, sticky="nsew", padx=12)
        # Click the Shown cell (✓ / –) to toggle a lab's visibility in the main
        # selector; no separate Show / hide button.
        self.tree.bind("<Button-1>", self._on_tree_click)

        tk.Label(presets_card,
                 text="Tip: click a lab's ✓ / – in the Shown column to show or hide it.",
                 bg=COLORS["card"], fg=COLORS["faint"], font=fonts.small, anchor="w").grid(
            row=3, column=0, sticky="w", padx=12, pady=(6, 0))

        btns = tk.Frame(presets_card, bg=COLORS["card"])
        btns.grid(row=4, column=0, sticky="ew", padx=12, pady=(8, 12))
        ttk.Button(btns, text="Add lab", command=self._add).pack(side="left")
        ttk.Button(btns, text="Edit", command=self._edit).pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="Delete", command=self._delete).pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="Close", command=self._close).pack(side="right")
        self.preset_status = tk.Label(presets_card, text="", bg=COLORS["card"],
                                      fg=COLORS["muted"], font=fonts.small, anchor="w",
                                      justify="left", wraplength=560)
        self.preset_status.grid(row=5, column=0, sticky="ew", padx=12, pady=(0, 10))

        self._reload_tree()
        _center_on(parent, top, divisor=6)
        try:
            _grab_modal(top)
        except tk.TclError:
            pass

    # -- which lab is this computer (item 8) --------------------------------

    def _build_identity_card(self, outer):
        card = tk.Frame(outer, bg=COLORS["card"], highlightthickness=1,
                        highlightbackground=COLORS["card_line"])
        card.grid(row=0, column=0, sticky="ew")
        card.columnconfigure(1, weight=1)
        tk.Label(card, text="Which lab is this computer", bg=COLORS["card"], fg=COLORS["text"],
                 font=self.fonts.bold, anchor="w").grid(
            row=0, column=0, columnspan=3, sticky="ew", padx=12, pady=(10, 2))
        tk.Label(card,
                 text="Sets the machine's lab (saved to lab.local) and shows only that lab in the "
                      "main selector. No file editing needed.",
                 bg=COLORS["card"], fg=COLORS["faint"], font=self.fonts.small, anchor="w",
                 justify="left", wraplength=560).grid(
            row=1, column=0, columnspan=3, sticky="ew", padx=12, pady=(0, 8))
        tk.Label(card, text="This computer", bg=COLORS["card"], fg=COLORS["muted"],
                 font=self.fonts.body, anchor="w").grid(
            row=2, column=0, sticky="w", padx=(12, 8), pady=(0, 10))
        self.identity_var = tk.StringVar(self.top, value="")
        self.identity_combo = ttk.Combobox(card, textvariable=self.identity_var, state="readonly")
        self.identity_combo.grid(row=2, column=1, sticky="ew", pady=(0, 10))
        ttk.Button(card, text="Make this the lab", command=self._apply_identity).grid(
            row=2, column=2, sticky="e", padx=(8, 12), pady=(0, 10))
        self.identity_status = tk.Label(card, text="", bg=COLORS["card"], fg=COLORS["muted"],
                                        font=self.fonts.small, anchor="w", justify="left",
                                        wraplength=560)
        self.identity_status.grid(row=3, column=0, columnspan=3, sticky="ew", padx=12, pady=(0, 10))
        self._refresh_identity()

    def _refresh_identity(self):
        presets = self._lab_presets()
        self._identity_id_by_name = {p["name"]: p["id"] for p in presets}
        self.identity_combo.configure(values=[p["name"] for p in presets])
        current = read_lab_marker()
        cur_preset = core.find_lab_preset(current, presets) if current else None
        if cur_preset is not None:
            self.identity_var.set(cur_preset["name"])
            self.identity_status.configure(
                text="This computer is set to %s." % cur_preset["name"], fg=COLORS["muted"])
        else:
            self.identity_var.set(presets[0]["name"] if presets else "")
            self.identity_status.configure(
                text="Not set yet: pick this computer's lab.", fg=COLORS["warn"])

    def _apply_identity(self):
        name = self.identity_var.get()
        lab_id = getattr(self, "_identity_id_by_name", {}).get(name)
        if not lab_id:
            self.identity_status.configure(text="Pick a lab first.", fg=COLORS["warn"])
            return
        self.app.set_machine_lab(lab_id)
        self._reload_tree()
        self._refresh_identity()
        self.identity_status.configure(text="This computer is now %s." % name, fg=COLORS["ok"])

    def _admin_row(self, card, r, label, widget):
        tk.Label(card, text=label, bg=COLORS["card"], fg=COLORS["muted"], font=self.fonts.body,
                 anchor="w").grid(row=r, column=0, sticky="w", padx=(12, 8), pady=3)
        widget.grid(row=r, column=1, sticky="ew", pady=3, padx=(0, 6))

    def _toggle_admin_pw(self):
        self.a_pw_entry.configure(show="" if self.show_pw.get() else MASK_CHAR)

    def _save_admin(self):
        self.app.store_extra["pg_admin"] = {
            "admin_username": self.a_user.get().strip(),
            "admin_password": self.a_pw.get(),
            "admin_host": self.a_host.get().strip(),
            "admin_port": self.a_port.get().strip(),
        }
        self.app._persist_store()
        self.admin_status.configure(text="Saved ✓", fg=COLORS["ok"])

    # -- database section: custom registry + default ------------------------

    def _build_custom_db_card(self, outer, row):
        """Part 2 of the Database section: the whole global registry of custom
        databases, each with its title and creator researcher (grey). You can
        ADD here; editing/removing is a future idea (append-only for now)."""
        card = tk.Frame(outer, bg=COLORS["card"], highlightthickness=1,
                        highlightbackground=COLORS["card_line"])
        card.grid(row=row, column=0, sticky="ew", pady=(12, 0))
        card.columnconfigure(0, weight=1)
        tk.Label(card, text="Custom databases", bg=COLORS["card"], fg=COLORS["text"],
                 font=self.fonts.bold, anchor="w").grid(row=0, column=0, sticky="ew",
                                                        padx=12, pady=(10, 2))
        tk.Label(card,
                 text="Every database created in the launcher, shared across all configs. The grey "
                      "name is who created it. You can add databases here; editing and removing "
                      "come later.",
                 bg=COLORS["card"], fg=COLORS["faint"], font=self.fonts.small, anchor="w",
                 justify="left", wraplength=560).grid(row=1, column=0, sticky="ew",
                                                      padx=12, pady=(0, 8))
        self.db_list_frame = tk.Frame(card, bg=COLORS["card"])
        self.db_list_frame.grid(row=2, column=0, sticky="ew", padx=12)
        self.db_list_frame.columnconfigure(0, weight=1)
        btns = tk.Frame(card, bg=COLORS["card"])
        btns.grid(row=3, column=0, sticky="ew", padx=12, pady=(8, 12))
        ttk.Button(btns, text="Add a database", command=self._add_database).pack(side="left")
        self._reload_db_list()

    def _reload_db_list(self):
        frame = getattr(self, "db_list_frame", None)
        if frame is None:
            return
        try:
            for child in list(frame.winfo_children()):
                child.destroy()
        except tk.TclError:
            return
        customs = core.known_databases_from_store(self.app.store_extra)
        if not customs:
            tk.Label(frame, text="No custom databases yet.", bg=COLORS["card"],
                     fg=COLORS["faint"], font=self.fonts.small, anchor="w").grid(
                row=0, column=0, sticky="ew", pady=2)
            return
        for i, entry in enumerate(customs):
            line = tk.Frame(frame, bg=COLORS["card"])
            line.grid(row=i, column=0, sticky="ew", pady=1)
            tk.Label(line, text=entry["title"], bg=COLORS["card"], fg=COLORS["text"],
                     font=self.fonts.body, anchor="w").pack(side="left")
            if entry.get("researcher"):
                tk.Label(line, text="   created by %s" % entry["researcher"], bg=COLORS["card"],
                         fg=COLORS["faint"], font=self.fonts.small, anchor="w").pack(side="left")

    def _add_database(self):
        # Persist any admin creds typed but not yet blurred, so the create flow
        # (which reads pg_admin) sees them; then open the create dialog and reload
        # this list once it has had a chance to register a new database.
        try:
            self._save_admin()
        except tk.TclError:
            pass
        self.app.create_database_dialog()
        try:
            self.top.after(500, self._reload_db_list)
        except tk.TclError:
            pass

    def _build_default_db_card(self, outer, row):
        """Part 3 of the Database section: which built-in database is the default
        (oTree default SQLite / lab shared Postgres), plus the "View settings.py
        block" tool that used to sit behind the gear."""
        card = tk.Frame(outer, bg=COLORS["card"], highlightthickness=1,
                        highlightbackground=COLORS["card_line"])
        card.grid(row=row, column=0, sticky="ew", pady=(12, 0))
        card.columnconfigure(0, weight=1)
        tk.Label(card, text="Default database", bg=COLORS["card"], fg=COLORS["text"],
                 font=self.fonts.bold, anchor="w").grid(row=0, column=0, sticky="ew",
                                                        padx=12, pady=(10, 2))
        tk.Label(card, text="Which built-in database a brand-new config starts on.",
                 bg=COLORS["card"], fg=COLORS["faint"], font=self.fonts.small, anchor="w",
                 justify="left", wraplength=560).grid(row=1, column=0, sticky="ew",
                                                      padx=12, pady=(0, 6))
        current = str(self.app.store_extra.get("default_database", core.DB_BUILTIN_LAB))
        self.default_db = tk.StringVar(self.top, value=current)
        for i, (db_id, label) in enumerate((
                (core.DB_BUILTIN_SQLITE, core.DB_BUILTIN_SQLITE_TITLE),
                (core.DB_BUILTIN_LAB, core.DB_BUILTIN_LAB_TITLE))):
            tk.Radiobutton(card, text=label, variable=self.default_db, value=db_id,
                           bg=COLORS["card"], fg=COLORS["text"],
                           activebackground=COLORS["card"], activeforeground=COLORS["text"],
                           font=self.fonts.body, anchor="w", highlightthickness=0,
                           command=self._save_default_db).grid(
                row=2 + i, column=0, sticky="w", padx=12, pady=1)
        tools = tk.Frame(card, bg=COLORS["card"])
        tools.grid(row=4, column=0, sticky="ew", padx=12, pady=(10, 12))
        ttk.Button(tools, text="View settings.py block…",
                   command=self._view_block).pack(side="left")

    def _save_default_db(self):
        self.app.store_extra["default_database"] = self.default_db.get()
        self.app._persist_store()

    def _view_block(self):
        self.app.show_block()

    # -- preset table ------------------------------------------------------

    def _lab_presets(self):
        return core.lab_presets_from_store(self.app.store_extra)

    def _reload_tree(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        for preset in self._lab_presets():
            self.tree.insert("", "end", iid=preset["id"], text=preset["name"],
                             values=(preset["ip"], len(preset["seats"]),
                                     "✓" if preset["display"] else "–"))

    def _selected_id(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _save_presets(self, new_list):
        self.app.store_extra["lab_presets"] = new_list
        self.app._persist_store()
        self._reload_tree()
        self.app.apply_lab_presets_change()

    def _add(self):
        def on_save(name, ip, seats, cols=0):
            ok, message, new_list, _preset = core.add_lab_preset(
                self._lab_presets(), name, ip, seats, cols=cols)
            if ok:
                self._save_presets(new_list)
                self.preset_status.configure(text=message, fg=COLORS["ok"])
            return ok, message
        LabPresetEditDialog(self.top, self.fonts, "Add a lab", None, on_save)

    def _edit(self):
        lab_id = self._selected_id()
        if not lab_id:
            self.preset_status.configure(text="Select a lab to edit.", fg=COLORS["warn"])
            return
        preset = core.find_lab_preset(lab_id, self._lab_presets())

        def on_save(name, ip, seats, cols=0):
            ok, message, new_list, _preset = core.update_lab_preset(
                self._lab_presets(), lab_id, name=name, ip=ip, seats=seats, cols=cols)
            if ok:
                self._save_presets(new_list)
                self.preset_status.configure(text=message, fg=COLORS["ok"])
            return ok, message
        LabPresetEditDialog(self.top, self.fonts, "Edit lab", preset, on_save)

    def _on_tree_click(self, event):
        """Toggle a lab's visibility when its Shown cell (✓ / –) is clicked."""
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != "#3":   # the "shown" column
            return
        lab_id = self.tree.identify_row(event.y)
        if not lab_id:
            return
        preset = core.find_lab_preset(lab_id, self._lab_presets())
        if preset is None:
            return
        ok, message, new_list = core.set_lab_display(
            self._lab_presets(), lab_id, not preset["display"])
        if ok:
            self._save_presets(new_list)
            self.preset_status.configure(text="", fg=COLORS["muted"])
        else:
            self.preset_status.configure(text=message, fg=COLORS["warn"])

    def _delete(self):
        lab_id = self._selected_id()
        if not lab_id:
            self.preset_status.configure(text="Select a lab to delete.", fg=COLORS["warn"])
            return
        preset = core.find_lab_preset(lab_id, self._lab_presets())
        if not messagebox.askyesno(
                "Delete lab", "Delete the lab %r? This cannot be undone." % preset["name"],
                parent=self.top):
            return
        current = self.app.var["lab"].get()
        ok, message, new_list, next_selected = core.delete_lab_preset(
            self._lab_presets(), lab_id, selected_lab=current)
        if not ok:
            self.preset_status.configure(text=message, fg=COLORS["warn"])
            return
        self._save_presets(new_list)
        # If the deleted lab was the selected one, switch to the lab core chose.
        if next_selected and next_selected != current:
            self.app.var["lab"].set(next_selected)
        self.preset_status.configure(text=message, fg=COLORS["ok"])

    def _close(self):
        # Save the admin details on the way out, so a field edited without leaving
        # focus is never lost.
        try:
            self._save_admin()
        except tk.TclError:
            pass
        _modal_close(self.top)


# ---------------------------------------------------------------------------
# First-run setup wizard (shown only when lab_info.json is absent)
# ---------------------------------------------------------------------------


class FirstRunWizard(object):
    """Collect labs + database + admin and write lab_info.json.

    Shown once, when lab_info.json does not exist. There is deliberately NO
    app-name prompt (the app name is a fixed constant). On success it writes the
    file and sets self.ok True; the caller then reloads the lab info.
    """

    def __init__(self, root):
        self.root = root
        self.ok = False
        self.labs = []            # list of {"name","host","seats","map"}
        top = tk.Toplevel(root)
        self.top = top
        top.title("%s: first-time setup" % APP_NAME)
        # First run withdraws the main root before showing this wizard. A
        # transient Toplevel whose master is withdrawn never becomes viewable on
        # macOS/aqua, so a later wait_visibility() on it would block forever (the
        # frozen blank-Terminal, no-window symptom). Only tie it to the root when
        # the root is actually on screen; otherwise the wizard stands alone and
        # maps itself (see run_first_run_wizard).
        try:
            if root.winfo_viewable():
                top.transient(root)
        except tk.TclError:
            pass
        top.protocol("WM_DELETE_WINDOW", self._cancel)

        pad = {"padx": 10, "pady": 4}
        row = 0
        tk.Label(top, text="Set up this launcher for your lab",
                 font=("TkDefaultFont", 13, "bold")).grid(row=row, column=0, columnspan=4,
                                                          sticky="w", padx=10, pady=(10, 2))
        row += 1
        tk.Label(top, text=("No lab_info.json was found. Add your lab(s), the shared "
                            "database, and the admin login. You can edit lab_info.json "
                            "later, or add more labs from Lab Settings."),
                 justify="left", wraplength=560).grid(row=row, column=0, columnspan=4,
                                                      sticky="w", padx=10, pady=(0, 8))
        row += 1

        # --- Labs -----------------------------------------------------------
        tk.Label(top, text="Labs", font=("TkDefaultFont", 11, "bold")).grid(
            row=row, column=0, columnspan=4, sticky="w", padx=10, pady=(6, 0))
        row += 1
        self.lab_list = tk.Listbox(top, height=4, width=70)
        self.lab_list.grid(row=row, column=0, columnspan=3, sticky="ew", **pad)
        tk.Button(top, text="Remove", command=self._remove_lab).grid(
            row=row, column=3, sticky="ew", **pad)
        row += 1

        form = tk.Frame(top)
        form.grid(row=row, column=0, columnspan=4, sticky="ew", padx=10, pady=(0, 4))
        tk.Label(form, text="Name").grid(row=0, column=0, sticky="w")
        tk.Label(form, text="Host / IP").grid(row=0, column=1, sticky="w")
        tk.Label(form, text="Map").grid(row=0, column=2, sticky="w")
        self.w_name = tk.Entry(form, width=18)
        self.w_name.grid(row=1, column=0, padx=(0, 6))
        self.w_host = tk.Entry(form, width=18)
        self.w_host.grid(row=1, column=1, padx=(0, 6))
        self.w_map = ttk.Combobox(form, width=16, state="readonly",
                                  values=["(plain grid)"] + core.available_maps())
        self.w_map.set("(plain grid)")
        self.w_map.grid(row=1, column=2, padx=(0, 6))
        tk.Button(form, text="Add lab", command=self._add_lab).grid(row=1, column=3)
        tk.Label(form, text="Seats (one per line, or space/comma separated):").grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self.w_seats = tk.Text(form, width=68, height=3)
        self.w_seats.grid(row=3, column=0, columnspan=4, sticky="ew")
        row += 1

        # --- Database -------------------------------------------------------
        tk.Label(top, text="Shared database (PostgreSQL)",
                 font=("TkDefaultFont", 11, "bold")).grid(
            row=row, column=0, columnspan=4, sticky="w", padx=10, pady=(8, 0))
        row += 1
        dbf = tk.Frame(top)
        dbf.grid(row=row, column=0, columnspan=4, sticky="ew", padx=10)
        self.db_vars = {}
        for i, (key, label, default) in enumerate([
                ("db_name", "Database", "otree"), ("db_user", "User", "otree"),
                ("db_password", "Password", ""), ("db_host", "Host", "localhost"),
                ("db_port", "Port", "5432")]):
            tk.Label(dbf, text=label).grid(row=0, column=i, sticky="w")
            var = tk.StringVar(dbf, value=default)
            show = "*" if key == "db_password" else ""
            tk.Entry(dbf, textvariable=var, width=13, show=show).grid(
                row=1, column=i, padx=(0, 6))
            self.db_vars[key] = var
        row += 1

        # --- Admin ----------------------------------------------------------
        tk.Label(top, text="Admin login", font=("TkDefaultFont", 11, "bold")).grid(
            row=row, column=0, columnspan=4, sticky="w", padx=10, pady=(8, 0))
        row += 1
        af = tk.Frame(top)
        af.grid(row=row, column=0, columnspan=4, sticky="ew", padx=10)
        tk.Label(af, text="Username").grid(row=0, column=0, sticky="w")
        tk.Label(af, text="Password").grid(row=0, column=1, sticky="w")
        self.admin_user = tk.StringVar(af, value="admin")
        self.admin_pw = tk.StringVar(af, value="")
        tk.Entry(af, textvariable=self.admin_user, width=18).grid(row=1, column=0, padx=(0, 6))
        tk.Entry(af, textvariable=self.admin_pw, width=18, show="*").grid(row=1, column=1)
        row += 1

        # --- Buttons --------------------------------------------------------
        self.status = tk.Label(top, text="", fg="#b00", justify="left", wraplength=560)
        self.status.grid(row=row, column=0, columnspan=4, sticky="w", padx=10, pady=(8, 0))
        row += 1
        btns = tk.Frame(top)
        btns.grid(row=row, column=0, columnspan=4, sticky="e", padx=10, pady=10)
        tk.Button(btns, text="Use example values (localhost)",
                  command=self._use_example).pack(side="left", padx=(0, 8))
        tk.Button(btns, text="Cancel", command=self._cancel).pack(side="left", padx=(0, 8))
        tk.Button(btns, text="Create lab_info.json", command=self._create).pack(side="left")

        top.columnconfigure(0, weight=1)
        _center_over(top, root)

    # -- helpers -----------------------------------------------------------
    def _parse_seats(self):
        raw = self.w_seats.get("1.0", "end")
        parts = re.split(r"[\s,]+", raw.strip())
        return [p for p in parts if p]

    def _add_lab(self):
        name = self.w_name.get().strip()
        host = self.w_host.get().strip()
        seats = self._parse_seats()
        if not name:
            self.status.configure(text="Give the lab a name.")
            return
        if not seats:
            self.status.configure(text="Add at least one seat for the lab.")
            return
        map_choice = self.w_map.get()
        map_name = "" if map_choice == "(plain grid)" else map_choice
        self.labs.append({"name": name, "host": host, "seats": seats, "map": map_name})
        self.lab_list.insert("end", "%s · %s · %d seats%s" % (
            name, host or "(no host)", len(seats),
            "" if not map_name else " · map: " + map_name))
        self.w_name.delete(0, "end")
        self.w_host.delete(0, "end")
        self.w_seats.delete("1.0", "end")
        self.w_map.set("(plain grid)")
        self.status.configure(text="")

    def _remove_lab(self):
        sel = list(self.lab_list.curselection())
        for index in reversed(sel):
            self.lab_list.delete(index)
            del self.labs[index]

    def _collect_and_save(self, data):
        core.save_lab_info(data)
        self.ok = True
        _modal_close(self.top)

    def _use_example(self):
        example = core.load_example_lab_info()
        if not example:
            self.status.configure(text="Could not read lab_info.example.json.")
            return
        example.pop("_comment", None)
        self._collect_and_save(example)

    def _create(self):
        if not self.labs:
            self.status.configure(text="Add at least one lab first (fill the row, then Add lab).")
            return
        database = {k: v.get() for k, v in self.db_vars.items()}
        admin = {"username": self.admin_user.get(), "password": self.admin_pw.get()}
        data = core.build_lab_info(self.labs, database, admin)
        self._collect_and_save(data)

    def _cancel(self):
        self.ok = False
        _modal_close(self.top)


def _center_over(win, parent):
    try:
        win.update_idletasks()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - win.winfo_width()) // 2)
        y = parent.winfo_rooty() + 60
        win.geometry("+%d+%d" % (max(x, 0), max(y, 0)))
    except tk.TclError:
        pass


def run_first_run_wizard(root):
    """Show the first-run wizard modally. Returns True if lab_info.json was
    written, False if the user cancelled."""
    wizard = FirstRunWizard(root)
    top = wizard.top
    # The main root is withdrawn during first run, so the wizard cannot lean on a
    # visible master to be mapped for it. Map and raise it itself: deiconify,
    # bring it to the front, and flash -topmost so it is not born behind the
    # Terminal on macOS. Without this the window could stay unmapped and the grab
    # never take. Guarded so a platform that refuses -topmost still shows it.
    try:
        top.deiconify()
        top.update_idletasks()
        top.lift()
        top.attributes("-topmost", True)
        top.after(300, lambda: _clear_topmost(top))
        top.focus_force()
    except tk.TclError:
        pass
    _grab_modal(top)
    root.wait_window(top)
    return wizard.ok


def _clear_topmost(top):
    try:
        top.attributes("-topmost", False)
    except tk.TclError:
        pass


# On Windows the windowless launcher runs under pythonw.exe (no console), so a
# startup crash before the GUI appears would be invisible. This is the safety
# net: point stdout/stderr at a log file and record any unhandled startup
# exception there. A normal run with a real console is left untouched (the
# Activity Log panel shows runtime output there). The log lives in the app's
# data/ folder like everything else the launcher writes, and falls back to the
# user's home folder only if data/ cannot be created or written.
_CRASH_LOG_RESOLVED = None


def _resolve_crash_log_path():
    """data/otree-lab-launcher.log, created on demand; falls back to
    ~/otree-lab-launcher.log only if data/ cannot be created or written.
    Resolved once and cached so every writer agrees on one path."""
    global _CRASH_LOG_RESOLVED
    if _CRASH_LOG_RESOLVED is not None:
        return _CRASH_LOG_RESOLVED
    data_target = os.path.join(data_dir(), "otree-lab-launcher.log")
    try:
        os.makedirs(data_dir(), exist_ok=True)
        with open(data_target, "a", encoding="utf-8"):
            pass
        _CRASH_LOG_RESOLVED = data_target
    except OSError:
        _CRASH_LOG_RESOLVED = os.path.join(
            os.path.expanduser("~"), "otree-lab-launcher.log")
    return _CRASH_LOG_RESOLVED


def _install_crash_log():
    """Redirect stdout/stderr to the crash log when they are missing.

    Under pythonw both are ``None``; a stray ``print`` would then raise. We only
    touch a stream that is ``None`` so a normal console launch is unaffected.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        stream = open(_resolve_crash_log_path(), "a", buffering=1, encoding="utf-8")
    except OSError:
        return
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def _log_startup_crash(exc):
    try:
        import traceback
        with open(_resolve_crash_log_path(), "a", encoding="utf-8") as fh:
            fh.write("\n" + "=" * 60 + "\n")
            fh.write("otree_lab_launcher startup crash %s\n"
                     % _dt.datetime.now().isoformat())
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=fh)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Headless run (--run "<config>"): the live one-click shortcut's entry point.
# Loads a SAVED config from presets.json and runs the SAME launch sequence the
# GUI runs (env + DATABASE_URL + room + label file, resetdb, prodserver in its
# own terminal, then the authenticated dashboard open) with NO Tk window built.
# The launch logic itself is the shared code (build_env / prepare_label_file /
# resetdb_command / build_server_launch / core.open_dashboard_authenticated);
# this only drives it without a UI.
# ---------------------------------------------------------------------------


def _hlog(msg):
    """Print one headless-run progress line to stdout AND mirror it to the log.

    Under ``pythonw`` (the Windows no-console shortcut) ``_install_crash_log``
    has already pointed stdout at the crash log, so ``print`` alone lands in the
    log; we only append a second copy when stdout is a separate real console, to
    avoid duplicating every line into the file.
    """
    try:
        print(msg, flush=True)
    except (OSError, ValueError):
        pass
    try:
        crash_log = _resolve_crash_log_path()
        if getattr(sys.stdout, "name", None) != crash_log:
            with open(crash_log, "a", encoding="utf-8") as fh:
                fh.write(msg + "\n")
    except OSError:
        pass


def _headless_resetdb(path, env):
    """Run ``otree resetdb`` (answering the y) and stream its output. Returns the
    exit code, or ``None`` if it could not be started."""
    command = resetdb_command()
    _hlog("$ " + " ".join(command) + '     (answering "y" on stdin)')
    kwargs = {}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        process = subprocess.Popen(
            command, cwd=path, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, bufsize=1, **kwargs)
    except OSError as error:
        _hlog("Could not run otree resetdb: %s" % error)
        return None
    try:
        process.stdin.write("y\n")
        process.stdin.flush()
        process.stdin.close()
    except (OSError, ValueError):
        pass
    for line in process.stdout:
        line = line.rstrip()
        if line:
            _hlog(line)
    return process.wait()


def _headless_start_server(cfg, path, env):
    """Start ``otree prodserver`` in its own terminal, exactly as the GUI does
    (via build_server_launch). Returns True on success."""
    spec = build_server_launch(cfg, path, env)
    _hlog("Starting the server in a %s." % spec["description"])
    _hlog("$ " + " ".join(spec["cmd"][:2]) + (" ..." if len(spec["cmd"]) > 2 else ""))
    kwargs = {"cwd": path, "env": env}
    if spec["creationflags"]:
        kwargs["creationflags"] = spec["creationflags"]
    try:
        if spec["kind"] == "linux-background":
            process = subprocess.Popen(
                spec["cmd"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                universal_newlines=True, bufsize=1, **kwargs)
            threading.Thread(
                target=lambda: [_hlog(l.rstrip()) for l in process.stdout if l.rstrip()],
                daemon=True).start()
        else:
            subprocess.Popen(spec["cmd"], **kwargs)
    except OSError as error:
        _hlog("Could not start otree prodserver: %s" % error)
        return False
    _hlog("otree prodserver started.")
    return True


def headless_run(config_name, store_path=None):
    """Launch a SAVED config by name with no UI. Returns a process exit code.

    ``0`` means the server was started and the dashboard was opened. A missing
    config name is a loud, non-zero failure (so a broken shortcut is obvious, not
    a silent no-op): it lists the available config names on stderr and returns 2.
    """
    # Bring any pre-data/ presets.json + seats/ into data/ before loading them,
    # so an existing install's saved configs survive the switch to data/.
    core.migrate_legacy_data()
    store_path = store_path or presets_path()
    presets, store_extra = load_store(store_path)
    # This machine's lab identity configures the built-in default's lab, exactly
    # as it does at GUI startup; user configs are untouched.
    apply_lab_marker(presets)
    lab_presets = core.lab_presets_from_store(store_extra)

    wanted = str(config_name or "").strip()
    match = None
    for preset in presets:
        if str(preset.get("name", "")).strip() == wanted:
            match = preset
            break
    if match is None:
        names = [str(p.get("name", "")).strip() for p in presets
                 if str(p.get("name", "")).strip()]
        sys.stderr.write('ERROR: no saved config named "%s" in %s.\n' % (wanted, store_path))
        if names:
            sys.stderr.write("Available configs:\n")
            for name in names:
                sys.stderr.write("  - %s\n" % name)
        else:
            sys.stderr.write("There are no saved configs yet. Open the launcher and "
                             "save one first.\n")
        sys.stderr.flush()
        return 2

    cfg = normalize_config(match)
    _hlog("=" * 60)
    _hlog('%s headless run of saved config "%s"' % (APP_NAME, wanted))
    _hlog("Started %s" % _dt.datetime.now().isoformat(timespec="seconds"))

    path = cfg["project_path"].strip()
    if not path or not os.path.isdir(path):
        _hlog("ERROR: the config's project folder does not exist: %r" % path)
        return 3
    _hlog("Project folder: %s" % path)

    # 1. Settle the participant seat / label file for this run.
    try:
        label_file, note = core.prepare_label_file(cfg, wanted, lab_presets)
    except OSError as error:
        _hlog("ERROR: could not write the seat file: %s" % error)
        return 4
    _hlog(note)

    # 2. Resolve the environment (DATABASE_URL, admin/auth, room + label file).
    env = build_env(cfg, label_file=label_file)
    keys = launcher_env_keys(cfg, label_file=label_file)
    _hlog("Environment variables set for this run: %s" % ", ".join(keys))
    for key in keys:
        _hlog("    %s = %s" % (key, describe_env_value(key, env.get(key, ""))))
    if cfg["db_mode"] == DB_MODE_NONE:
        _hlog("    DATABASE_URL is not set at all, so oTree uses its own default.")

    # 3. Reset the database if the config asks for it.
    if cfg["resetdb"]:
        code = _headless_resetdb(path, env)
        if code is None:
            return 5
        if code != 0:
            _hlog("ERROR: otree resetdb exited with code %d; the server was not started."
                  % code)
            return 5
        _hlog("otree resetdb finished with exit code 0.")
    else:
        _hlog("Reset database is off, so otree resetdb was skipped.")

    # 4. Start the server in its own terminal window (the wanted Launch terminal).
    if not _headless_start_server(cfg, path, env):
        return 6

    # Record the run on the saved config, exactly like the GUI's _stamp_last_run.
    try:
        match["last_run"] = core.now_iso()
        save_store(presets, store_extra, store_path)
    except OSError:
        pass

    # 5. Open the admin dashboard already authenticated (auto-login on by default,
    #    the GUI default), via the SAME core entry point the GUI uses.
    use_auto = cfg.get("auto_login", True)
    if cfg["open_browser"]:
        _hlog("Waiting for the server to respond, then opening the dashboard.")
        result = core.open_dashboard_authenticated(
            core.AUTOLOGIN_HOST, cfg["port"], cfg["room_name"],
            cfg["admin_username"], cfg["admin_password"], auto_login=use_auto)
        if result.get("method") == "cookie":
            _hlog("Opened the dashboard already logged in (auto-login: form-login + "
                  "cookie relay): %s" % result.get("monitor_url"))
        else:
            _hlog("Opened the dashboard login page: %s" % result.get("monitor_url"))
            _hlog("    %s" % result.get("reason", ""))
    else:
        monitor_url = "http://%s:%s%s" % (
            core.AUTOLOGIN_HOST, cfg["port"], core.room_monitor_path(cfg["room_name"]))
        _hlog("Open in browser is off. The monitor page would be %s" % monitor_url)

    _hlog("Done. The server keeps running in its own window.")
    return 0


def main():
    # Pull any pre-data/ files (lab.local, lab_info.json, presets.json, seats/)
    # into data/ before anything reads them, then refresh so a just-migrated
    # lab_info.json is seen as present (no spurious first-run wizard).
    core.migrate_legacy_data()
    core.reload_lab_info()
    refresh_defaults_from_core()
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", root.tk.call("tk", "scaling"))
    except tk.TclError:
        pass
    # First run on this machine (or checkout): no lab_info.json yet. Collect the
    # lab-specific data and write it before the main window is built.
    if not core.lab_info_present():
        root.withdraw()
        if not run_first_run_wizard(root):
            root.destroy()
            return
        core.reload_lab_info()
        refresh_defaults_from_core()
        root.deiconify()
    LauncherApp(root)
    root.mainloop()


def _cli_config_name(argv):
    """The name after ``--run`` on the command line, or None for the GUI.

    Kept deliberately tiny so no UI is imported to decide it. Accepts both
    ``--run "Name"`` and ``--run=Name``.
    """
    for index, token in enumerate(argv):
        if token == "--run":
            return argv[index + 1] if index + 1 < len(argv) else ""
        if token.startswith("--run="):
            return token[len("--run="):]
    return None


if __name__ == "__main__":
    _install_crash_log()
    _run_name = _cli_config_name(sys.argv[1:])
    if _run_name is not None:
        # Headless one-click shortcut path: never construct Tk.
        try:
            sys.exit(headless_run(_run_name))
        except Exception as exc:  # noqa: BLE001 - report, don't vanish silently
            _log_startup_crash(exc)
            sys.stderr.write("Headless run failed: %s: %s\n" % (type(exc).__name__, exc))
            sys.exit(1)
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - last-resort startup diagnostics
        _log_startup_crash(exc)
        raise
