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
# Storage file names/versions live only in otree_core (1.5.0: saved_configs.json,
# machine.json, lab_info.json, each with a schema_version).

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

# The default room a lab uses. Editable per lab (wizard + Lab Settings); this is
# only the fallback when a lab sets none.
DEFAULT_ROOM_NAME = "study"

# Shared help text for the per-lab "Default room" field (wizard + Lab Settings):
# the wording lives in core (core.ROOM_HELP) so both faces match; kept as an alias.
ROOM_TOOLTIP_TEXT = core.ROOM_HELP

# The tip next to "Production mode" (core.UI_TIPS, shared with the web face).
PRODUCTION_TIP = core.ui_tip("production_mode")

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

# The Tk launcher now SHARES core's config model rather than keeping its own copy
# (fable review item 10, step 1): DEFAULT_CONFIG IS core.DEFAULT_CONFIG (the same
# object, not a copy) and FIELD_KEYS IS core.FIELD_KEYS. core.reload_lab_info /
# core.apply_default_database update that one dict in place, so the Tk defaults
# track lab_info.json / the chosen lab-shared database with no separate refresh,
# and the old wait_seconds 2-vs-5 drift between the two faces (which made a
# Tk-saved config report phantom "unsaved changes" in the web one-click shortcut,
# item 2) is gone. auto_login (item 1) rides along because it is in core's model.
DEFAULT_CONFIG = core.DEFAULT_CONFIG

# The user-editable settings of a config.  Anything outside this list
# (name, created, last_run, plus keys written by a future version) is
# metadata and is never compared or overwritten.
FIELD_KEYS = core.FIELD_KEYS


def refresh_defaults_from_core():
    """Retained no-op for its existing callers.

    DEFAULT_CONFIG is now the SAME object as core.DEFAULT_CONFIG (see above), and
    core.reload_lab_info / core.apply_default_database update it in place, so the
    Tk defaults already reflect any lab_info.json / default-database change with
    nothing to re-pull here. Kept so the call sites (main, dialogs) stay valid."""
    return

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


# De-duplicated (item #10): these were byte-for-byte copies of core's. The Tk
# module names now point at the single implementation so the two faces can never
# drift on the config model; the parity suite guards the rest of the tail.
normalize_config = core.normalize_config
configs_differ = core.configs_differ


# De-duplicated (item #10): the DATABASE_URL builder, the log-masking of a URL
# password and the auto-open URL builder were byte-for-byte copies of core's.
build_database_url = core.build_database_url
mask_database_url = core.mask_database_url


def resolve_host(cfg):
    # Delegates to core, which resolves against the labs in lab_info.json.
    return core.resolve_host(cfg)


build_url = core.build_url


def build_env(cfg, base_env=None, label_file=None):
    """A copy of the process environment with this config's values applied.

    De-duplicated: the launch environment is built ONCE in otree_core so a fix
    to the env (a stale-var removal, a new OTREE_* var, the room/seat rule) lands
    in a single place. This is a thin passthrough, byte-for-byte identical to the
    previous local copy (a parity test asserts it against ``core.build_env``).
    """
    return core.build_env(cfg, base_env, label_file)


def launcher_env_keys(cfg, label_file=None):
    """The names of the variables this config actually sets, in order.

    Delegates to :func:`otree_core.launcher_env_keys` so the key list can never
    drift from :func:`build_env`; the two live together in core.
    """
    return core.launcher_env_keys(cfg, label_file)


# De-duplicated (item #10): identical to core's log-safe env renderer.
describe_env_value = core.describe_env_value


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


# De-duplicated (item #10): core's seat helpers carry an extra optional
# ``lab_presets`` argument (defaulting to None) that the Tk copies lacked, so a
# bare alias keeps every existing Tk caller working while sharing one
# implementation. The ``LauncherApp`` wrappers that DO know the lab presets pass
# them positionally, which the aliased signatures accept unchanged.
resolve_seats = core.resolve_seats


def effective_seat_mode(cfg):
    """The seat mode a launch will REALLY use, after resolving empties to None.

    Seats are never a hard block (Julian): an absent seat file, a file that
    cannot be read or is empty, or a lab-default/edit selection that resolves to
    no seats all fall back to SEAT_NONE, a valid open-room launch. A chosen file
    that DOES have labels stays SEAT_FILE (so its labels can still be validated).
    Delegates to core so both launchers agree.
    """
    return core.effective_seat_mode(cfg)


# De-duplicated (item #10): the remaining seat helpers were byte-for-byte copies
# of core's (seat_summary/prepare_label_file gain the same optional lab_presets
# tail as resolve_seats; a bare alias keeps existing Tk callers working).
invalid_seats = core.invalid_seats
seat_file_text = core.seat_file_text
seats_dir = core.seats_dir
seat_file_path = core.seat_file_path
write_seat_file = core.write_seat_file
read_seat_file = core.read_seat_file
seat_summary = core.seat_summary
prepare_label_file = core.prepare_label_file
seat_preview = core.seat_preview


# ---------------------------------------------------------------------------
# The settings.py block
# ---------------------------------------------------------------------------

# The settings.py block is defined ONCE in otree_core (which is kept byte-for-
# byte in sync with otree_lab_block.py, the documented reference the sync test
# compares). The Tk launcher used to carry its own identical copy; it now points
# at core so a change to the block only ever has to be made in one place.
BLOCK_MARKER = core.BLOCK_MARKER
BLOCK_END_MARKER = core.BLOCK_END_MARKER
LAB_BLOCK = core.LAB_BLOCK


def settings_path_for(project_path):
    return os.path.join((project_path or "").strip(), "settings.py")


def inspect_settings(project_path, lab_room=None):
    """Look for the lab support block in a project's settings.py.

    De-duplicated: delegates to the single implementation in
    :func:`otree_core.inspect_settings`, so the Tk and web faces share one
    detector (including the ``lab_room`` comparison and the ``stale`` /
    ``needs_refresh`` signals). ``lab_room`` is this lab's default room ("study"
    fallback).
    """
    return core.inspect_settings(project_path, lab_room)


def append_block(project_path):
    """Append the lab support block to settings.py. Returns (backup, settings).

    Delegates to the SINGLE atomic, lock-guarded implementation in
    ``core.append_block`` (re-reads + re-checks the marker under an exclusive
    lock, unique .bak name, temp+fsync+os.replace), so the Tk and web faces
    share one safe writer for the one place the launcher mutates a researcher's
    own code. Raises ``core.BlockAlreadyPresent`` if the block is already there.
    """
    return core.append_block(project_path)


def refresh_block(project_path):
    """Atomically replace an outdated lab support block with the current one.

    Delegates to :func:`otree_core.refresh_block` (same lock + timestamped .bak +
    temp/fsync/os.replace machinery as append_block): it removes the old block
    region and appends the current ``core.LAB_BLOCK``. Fixes a project stuck on an
    old/stale block, which ``append_block`` cannot (it refuses when a marker is
    present). Returns ``(backup, settings)``.
    """
    return core.refresh_block(project_path)


# ---------------------------------------------------------------------------
# Project folder validation
# ---------------------------------------------------------------------------


# De-duplicated (item #10): identical to core's project-folder validation.
validate_project = core.validate_project
find_app_packages = core.find_app_packages


def titlecase_app(name):
    """A readable, title-cased app name, e.g. 'public_goods' -> 'Public Goods'."""
    return re.sub(r"[_-]+", " ", name).strip().title()


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


# De-duplicated (item #10): the storage-path helpers were mirrors of core's and
# resolve to the SAME data/ folder at the repo root (core's __file__ lives in the
# same app/ directory), so a bare alias keeps every path identical.
repo_root = core.repo_root
app_dir = core.app_dir
data_dir = core.data_dir
config_dir = core.config_dir
presets_path = core.presets_path


# --- This computer's lab (machine.json home_lab) ---------------------------
# Which lab this PC is, chosen in the setup wizard or Lab Settings. Bare aliases
# of core's functions (the lab.local names are kept; the value now lives in
# machine.json). write_lab_marker is the no-clobber first write, set_lab_marker
# the change-it-later overwrite.
lab_marker_path = core.lab_marker_path
read_lab_marker = core.read_lab_marker
set_lab_marker = core.set_lab_marker
write_lab_marker = core.write_lab_marker
apply_lab_marker = core.apply_lab_marker


# De-duplicated (item #10): identical to core's built-in test.
is_builtin = core.is_builtin


def lab_suffix(lab):
    """The derived, never-stored name suffix for a config's lab. Delegates to
    core, which names the lab from its lab_info.json preset."""
    return core.lab_suffix(lab)


# De-duplicated (item #10): the display-name rule and the built-in default
# factory were byte-for-byte copies of core's (both now source their defaults
# from the shared DEFAULT_CONFIG and the same home-lab setting in machine.json).
display_name = core.display_name
default_preset = core.default_preset


def clear_builtin_last_run(presets):
    """Force the built-in Lab default's ``last_run`` to None, in place (Job 2).

    The built-in "Lab default" is a launch TEMPLATE, never a saved config, so it
    must NEVER record a run; this clears any ``last_run`` a previous version
    stamped onto it on load. Mirror of otree_core.clear_builtin_last_run."""
    return core.clear_builtin_last_run(presets)


def clear_builtin_project_path(presets):
    """Force the built-in Lab default's ``project_path`` to "" on load, so it
    always opens Browse-first with no folder set (Job 2). Mirror of
    otree_core.clear_builtin_project_path."""
    return core.clear_builtin_project_path(presets)


def load_store(path=None):
    """Read the presets file.

    De-duplicated: the parse / broken-file-backup / unnamed-config logic lives
    once in :func:`otree_core.load_store`. This passthrough supplies the Tk
    launcher's own :func:`default_preset` as the fallback factory so the returned
    "Lab default" keeps this face's defaults (e.g. its browser-open delay), while
    the file handling itself is shared.
    """
    return core.load_store(path, default_factory=default_preset)


def save_store(presets, extra=None, path=None):
    """Write the presets file atomically.

    De-duplicated: the atomic, owner-only (0o600) write lives once in
    :func:`otree_core.save_store`; the Tk launcher used to keep its own copy of
    the same temp-file-then-replace sequence. Byte-for-byte identical output (a
    parity test asserts a saved-then-loaded round-trip matches core's).
    """
    return core.save_store(presets, extra, path)


def sort_presets(presets):
    """Built-in (Lab default) first, then most recently run, then by name.

    De-duplicated (Pass 5): the ordering lives once in
    :func:`otree_core.order_presets_for_display` so the Tk sidebar and the web
    config list agree exactly. Kept under this name because the launcher and its
    tests call ``sort_presets``.
    """
    return core.order_presets_for_display(presets)


# De-duplicated (item #10): identical to core's name-uniqueness test, the
# fields->preset factory and the "last run ..." formatter. (core's
# preset_from_fields fills a blank author from getpass.getuser() the same way the
# Tk default_author() did; every Tk caller passes an explicit author or None.)
unique_name = core.unique_name
preset_from_fields = core.preset_from_fields
format_last_run = core.format_last_run


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def resetdb_command():
    """`otree resetdb`, exactly as the batch file ran it.

    De-duplicated: the exact resetdb argv lives once in core.
    """
    return core.resetdb_command()


def _prodserver_argv(cfg):
    """``["otree", "prodserver"]`` plus the validated port when one is set.

    Delegates to core so the server command, the port preflight and the opened
    URLs all share the one :func:`otree_core.launch_port`; a blank/invalid port
    omits the arg and lets oTree default to 8000. Kept because export_bat_text
    still composes the command line from it.
    """
    return core._prodserver_argv(cfg)


def macos_shell_script(cfg, project_path, env_pairs):
    """The shell line that a new macOS Terminal window will run (core-owned)."""
    return core.macos_shell_script(cfg, project_path, env_pairs)


def build_server_launch(cfg, project_path, env, platform_name=None):
    """How to start `otree prodserver` in a window the researcher can see.

    De-duplicated: the per-platform launch spec (Windows ``cmd /k``, macOS
    ``osascript``/Terminal, Linux terminal-or-background) is built once in
    :func:`otree_core.build_server_launch`. This passthrough is byte-for-byte
    identical to the previous local copy (a parity test asserts it across all
    three platforms), so the configured port and env still reach prodserver the
    same way on every platform.
    """
    return core.build_server_launch(cfg, project_path, env, platform_name)


# ---------------------------------------------------------------------------
# Batch file export
# ---------------------------------------------------------------------------


# De-duplicated (item #10, last): the .bat export was a byte-for-byte copy of
# core's and is unreachable from either UI (no button calls it; only the tests
# do), so sharing it is pure tidiness. Kept under this name for those tests.
export_bat_text = core.export_bat_text


# ---------------------------------------------------------------------------
# Look
# ---------------------------------------------------------------------------

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
# Loads a SAVED config from saved_configs.json and runs the SAME launch sequence the
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
    (via build_server_launch). Returns ``(ok, process)``: ``ok`` is True on a
    successful spawn; ``process`` is the linux-background child (so the caller can
    surface an early exit) or None on the terminal paths / on failure."""
    spec = build_server_launch(cfg, path, env)
    _hlog("Starting the server in a %s." % spec["description"])
    _hlog("$ " + " ".join(spec["cmd"][:2]) + (" ..." if len(spec["cmd"]) > 2 else ""))
    kwargs = {"cwd": path, "env": env}
    if spec["creationflags"]:
        kwargs["creationflags"] = spec["creationflags"]
    process = None
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
        return False, None
    _hlog("otree prodserver started.")
    return True, process


def _headless_startup_failure_message(process=None):
    """Message logged when a headless prodserver never became ready: point at the
    server terminal window (real traceback) and surface an early child exit when
    the linux-background handle shows one."""
    base = ("ERROR: the oTree server did not become ready — it may have crashed on "
            "startup (a missing project, a database error, or the port already in "
            "use). Nothing was marked as launched. Check the server terminal window "
            "for the real error.")
    try:
        if process is not None:
            code = process.poll()
            if code is not None:
                base += " (The server process already exited with code %s.)" % code
    except Exception:
        pass
    return base


def _ask_headless_launch_anyway(text):
    """The one-click shortcut's question box: ``text`` lists the problems and
    ends with "Launch anyway?". Returns True to launch, False to cancel.

    macOS: an alert with the buttons "Cancel" / "Launch anyway" (Cancel is the
    default). Windows: a message box with Yes / No (No is the default; the text
    ends with the question, so Yes = launch anyway). Anywhere else there is no
    way to ask: the problems are written to the log and the launch goes ahead,
    as it did before this question existed. Never raises."""
    try:
        if sys.platform.startswith("win"):
            import ctypes
            # MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2 | MB_SETFOREGROUND; IDYES = 6
            answer = ctypes.windll.user32.MessageBoxW(
                0, str(text), core.HEADLESS_ASK_TITLE, 0x04 | 0x30 | 0x100 | 0x10000)
            return answer == 6
        if sys.platform == "darwin":
            script = ('display alert %s message %s as warning buttons '
                      '{"Cancel", "Launch anyway"} default button "Cancel" '
                      'cancel button "Cancel"'
                      % (core._applescript_string(core.HEADLESS_ASK_TITLE),
                         core._applescript_string(str(text))))
            result = subprocess.run(["osascript", "-e", script],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            output = result.stdout.decode("utf-8", "replace") \
                if isinstance(result.stdout, (bytes, bytearray)) else str(result.stdout or "")
            return result.returncode == 0 and "Launch anyway" in output
    except Exception:
        return True
    return True


def headless_run(config_name, store_path=None, ask=None, problem_finder=None):
    """Launch a SAVED config by name with no UI. Returns a process exit code.

    ``0`` means the server was started and the dashboard was opened. A missing
    config name is a loud, non-zero failure (so a broken shortcut is obvious, not
    a silent no-op): it lists the available config names on stderr and returns 2.
    ``9`` means a session is already running on the launch port, so nothing was
    reset or started (core.running_server_problem). ``10`` means the user chose
    Cancel in the question box (core.headless_ask_problems: a newer version of
    the study online, the database not reachable, the room not the lab default).
    ``ask(text) -> bool`` and ``problem_finder(cfg, lab_presets) -> [text]`` are
    test seams.
    """
    # Upgrade an old data folder first (same startup step as the GUI). A data
    # folder written by a NEWER launcher is refused: exit code 8.
    storage = core.prepare_storage()
    if storage.get("newer_schema"):
        sys.stderr.write("ERROR: %s\n" % (storage.get("message") or
                                          "data folder written by a newer launcher"))
        sys.stderr.flush()
        return 8
    store_path = store_path or presets_path()
    presets, store_extra = load_store(store_path)
    core.apply_default_database(store_extra)
    # This machine's lab identity configures the built-in default's lab AND its
    # room (from that lab's default_room), exactly as at GUI startup, so a shortcut
    # for the built-in "Lab default" re-derives the room the lab uses instead of
    # leaving the code default (fable review item 9). lab_presets is resolved
    # first so apply_lab_marker can read the room; user configs are untouched.
    lab_presets = core.lab_presets_from_store(store_extra)
    apply_lab_marker(presets, lab_presets=lab_presets)

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

    # A config saved with the lab's default room follows the lab's CURRENT
    # default room (core.follow_lab_room), so it never needs a room warning.
    cfg = normalize_config(core.follow_lab_room(match, lab_presets))
    _hlog("=" * 60)
    _hlog('%s headless run of saved config "%s"' % (APP_NAME, wanted))
    _hlog("Started %s" % _dt.datetime.now().isoformat(timespec="seconds"))
    if not storage.get("ok", True) and storage.get("message"):
        _hlog("WARNING: %s" % storage["message"])
    # The database: say so when the config's own database is not on this PC (the
    # PC default is used instead), or when a localhost database was made on
    # another computer.
    db_note = core.config_database_note(cfg)
    if db_note:
        _hlog("NOTE: %s" % db_note)
    db_entry = core.find_database(store_extra, core.current_database_id(cfg))
    db_warning = core.database_location_warning(db_entry) if db_entry else ""
    if db_warning:
        _hlog("WARNING: %s" % db_warning)

    path = cfg["project_path"].strip()
    if not path or not os.path.isdir(path):
        _hlog("ERROR: the config's project folder does not exist: %r" % path)
        return 3
    _hlog("Project folder: %s" % path)

    # 0. A server already answering on the launch port = a session is running.
    #    Refuse BEFORE the seat file or the database is touched (N1): a relaunch
    #    would reset the running session's database. Exit code 9.
    running = core.running_server_problem(cfg)
    if running:
        _hlog("ERROR: %s" % running)
        core.record_session(cfg, config_name=wanted, author=match.get("author", ""),
                            outcome="fail", lab_presets=lab_presets)
        return 9

    # 0b. With no window there is no launch screen to show warnings on, so for
    #     exactly three of them the shortcut STOPS AND ASKS, in one box: a newer
    #     version of the study online, the database not reachable, the room not
    #     the lab default. Everything else stays silent. Nothing has been
    #     written or reset yet.
    try:
        problems = (problem_finder or core.headless_ask_problems)(cfg, lab_presets)
    except Exception as error:  # noqa: BLE001 - the checks must never stop a launch
        _hlog("NOTE: the pre-launch checks could not run (%s)." % error)
        problems = []
    if problems:
        for problem in problems:
            _hlog("ASK: %s" % problem)
        if not (ask or _ask_headless_launch_anyway)(
                core.headless_ask_text(problems, wanted)):
            _hlog("Cancelled before launching: nothing was reset or started.")
            return core.HEADLESS_CANCELLED_EXIT
        _hlog("Launch anyway was chosen.")

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
    _hlog(core.env_log_line(keys, env))

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
    server_start = time.monotonic()
    ok, server_proc = _headless_start_server(cfg, path, env)
    if not ok:
        core.record_session(cfg, config_name=wanted, author=match.get("author", ""),
                            outcome="fail", lab_presets=lab_presets)
        return 6

    # 5. Open the admin dashboard already authenticated (auto-login on by default,
    #    the GUI default), via the SAME core entry point the GUI uses. The open
    #    also polls readiness and now RETURNS whether the server actually came up.
    use_auto = cfg.get("auto_login", True)
    if cfg["open_browser"]:
        _hlog("Waiting for the server to respond, then opening the dashboard.")
        # block_relay=True: this headless process exits the instant we return, so
        # wait for the one-shot cookie relay to actually serve the browser (bounded
        # by its idle_timeout) before exiting -- otherwise the daemon relay thread
        # dies before the browser connects and Safari shows "cannot connect to
        # localhost:<port>". The GUI/web paths leave block_relay at its default.
        result = core.open_dashboard_authenticated(
            core.AUTOLOGIN_HOST, cfg["port"], cfg["room_name"],
            cfg["admin_username"], cfg["admin_password"], auto_login=use_auto,
            block_relay=True,
            on_wait=lambda s: _hlog("Still waiting for the server to respond (%d s)…" % s))
        server_ready = bool(result.get("server_ready"))
        if result.get("method") == "cookie":
            _hlog("Opened the dashboard already logged in (auto-login: form-login + "
                  "cookie relay): %s" % result.get("monitor_url"))
        else:
            _hlog("Opened the dashboard login page: %s" % result.get("monitor_url"))
            _hlog("    %s" % result.get("reason", ""))
    else:
        monitor_url = "http://%s:%s%s" % (
            core.AUTOLOGIN_HOST, cfg["port"], core.room_monitor_path(cfg["room_name"]))
        _hlog("Open in browser is off. Confirming the server is up. The monitor page "
              "would be %s" % monitor_url)
        server_ready = core.wait_for_server(
            core.AUTOLOGIN_HOST, cfg["port"],
            on_wait=lambda s: _hlog("Still waiting for the server to respond (%d s)…" % s))

    if not server_ready:
        # A startup crash / missing project / port race: prodserver never
        # answered. Do NOT stamp last_run; report the honest failure and a non-zero
        # exit so a broken one-click shortcut is obvious. Fail-soft: a page may
        # already be open (manual fallback), so the operator can still retry.
        _hlog(_headless_startup_failure_message(server_proc))
        core.record_session(cfg, config_name=wanted, author=match.get("author", ""),
                            outcome="fail", lab_presets=lab_presets)
        return 7

    # Record the run on the saved config, exactly like the GUI's _stamp_last_run --
    # but ONLY now that the server is confirmed ready, and NEVER for the built-in
    # Lab default (it is a launch TEMPLATE, not a saved config -- Job 2).
    if not is_builtin(match):
        try:
            match["last_run"] = core.now_iso()
            save_store(presets, store_extra, store_path)
        except OSError:
            pass

    # A launch happened (server confirmed ready): append one session-log line
    # (fail-soft) alongside the last_run stamp, exactly like the GUI (review F).
    core.record_session(cfg, config_name=wanted, author=match.get("author", ""),
                        outcome="ok", server_ready_seconds=time.monotonic() - server_start,
                        lab_presets=lab_presets)

    _hlog("Done. The server keeps running in its own window.")
    return 0


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


def _notify_headless_failure(message):
    """Show ONE native message box on a headless one-click failure, so a
    windowless (pythonw) shortcut failure is not silent (fable review item 9).
    Windows: ctypes MessageBoxW; macOS: osascript 'display alert'; otherwise a
    stderr line. Never raises."""
    try:
        if sys.platform.startswith("win"):
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, str(message),
                                             "oTree Lab Launcher", 0x10)  # MB_ICONERROR
            return
        if sys.platform == "darwin":
            script = ('display alert "oTree Lab Launcher" message %s as critical'
                      % core._applescript_string(str(message)))
            subprocess.run(["osascript", "-e", script],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
    except Exception:
        pass
    try:
        sys.stderr.write(str(message) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Headless one-click shortcut dispatch, placed ABOVE the module-level tkinter
# import below, so a ``--run "<config>"`` shortcut runs and exits WITHOUT ever
# importing tkinter -- it builds no window, so it must work on a Python that has
# no Tk (fable review item 9). A normal GUI launch falls through to the tkinter
# import and the bottom-of-file main() dispatch.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    _install_crash_log()
    _run_name = _cli_config_name(sys.argv[1:])
    if _run_name is not None:
        try:
            _code = headless_run(_run_name)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 - report, don't vanish silently
            _log_startup_crash(exc)
            _notify_headless_failure(
                "The oTree Lab Launcher one-click shortcut failed to start: %s: %s"
                % (type(exc).__name__, exc))
            sys.stderr.write("Headless run failed: %s: %s\n" % (type(exc).__name__, exc))
            sys.exit(1)
        if _code == 9:
            # A session already answers on the launch port: say exactly that.
            _notify_headless_failure(core.HEADLESS_SESSION_RUNNING_MESSAGE)
        elif _code == core.HEADLESS_CANCELLED_EXIT:
            pass      # the user chose Cancel in the question box: nothing to report
        elif _code != 0:
            _notify_headless_failure(
                "The oTree Lab Launcher one-click shortcut could not launch "
                "(exit code %s). See the log for details:\n%s"
                % (_code, _resolve_crash_log_path()))
        sys.exit(_code)


import tkinter as tk  # noqa: E402  (kept below the logic so tests import cheaply)
from tkinter import filedialog, messagebox, simpledialog, ttk  # noqa: E402
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
        # A message ending in "See the activity log." shows a [See activity log]
        # button instead of that sentence, when the owner sets on_show_log.
        self.on_show_log = None
        self.log_button = ttk.Button(self, text=core.ACTIVITY_LOG_BUTTON_LABEL,
                                     command=self._show_log)

    def _show_log(self):
        if callable(self.on_show_log):
            self.on_show_log()

    def set(self, level, message, quiet=False):
        hint = False
        if self.on_show_log is not None:
            message, hint = core.split_activity_log_hint(message)
        if hint:
            self.log_button.pack(side="left", padx=(8, 0), anchor="n")
        else:
            self.log_button.pack_forget()
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

    def toggle(self, _event=None):
        """Show the tip at once, or hide it when it is showing (a click)."""
        if self.window is not None:
            self._hide()
        else:
            self._cancel()
            self._show()


def run_in_background(widget, work, done, poll_ms=100, limit=6000):
    """Run ``work()`` on a daemon thread and call ``done(result)`` on the UI
    thread. The MAIN thread polls for the result (``widget.after``), so no Tk
    call is ever made from the worker: ``after()`` called from a worker thread
    is not delivered on every Tcl build. ``done`` is skipped when ``widget`` is
    gone by then. An exception in ``work`` gives ``done(None)``."""
    box = {}

    def runner():
        try:
            box["result"] = work()
        except Exception:                      # the caller shows its own fallback
            box["result"] = None
        box["ready"] = True

    def poll(tries=0):
        try:
            if not widget.winfo_exists():
                return
        except tk.TclError:
            return
        if box.get("ready"):
            done(box.get("result"))
            return
        if tries > limit:
            return
        try:
            widget.after(poll_ms, lambda: poll(tries + 1))
        except (tk.TclError, RuntimeError):
            pass

    threading.Thread(target=runner, name="background", daemon=True).start()
    poll()


def info_tip(parent, text, bg=None, font=None):
    """THE info tip of the Tk face: a small "ⓘ" that shows ``text`` on hover
    (after 100 ms) and on click. Every explanation that is not an instruction
    lives behind one of these instead of a visible grey sentence (the wording
    comes from ``core.ui_tip``). ``text`` may be a string or a zero-argument
    callable (for a tip whose text changes). Returns the label, un-placed: the
    caller packs/grids it right after the label or heading it explains."""
    if bg is None:
        try:
            bg = parent.cget("bg")
        except tk.TclError:
            bg = COLORS["card"]
    provider = text if callable(text) else (lambda: text)
    label = tk.Label(parent, text=" " + glyph_or_fallback("\u24d8", "(i)"), bg=bg,
                     fg=COLORS["accent"], cursor="hand2", bd=0, padx=0, pady=0)
    if font is not None:
        label.configure(font=font)
    tip = Tooltip(label, provider, delay=100)
    label.bind("<Enter>", tip._schedule, add="+")
    label.bind("<Leave>", tip._hide, add="+")
    label.bind("<Button-1>", tip.toggle, add="+")
    label.tooltip = tip
    label.info_text = provider
    label.is_info_tip = True
    return label


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
        # One cap for every room (it was 38 for a wide room, which made the large
        # lab's map visibly smaller than the small lab's): the card width is what
        # limits a wide room, not an arbitrary smaller square (Julian, 2026-10-01).
        maxcell = 46
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

        # ONE in-process lock for every read-modify-write-save of the store
        # (self.presets / self.store_extra). Tk UI actions run on the main
        # thread, but the launch worker (last_run stamp) and the create-database
        # worker also mutate-and-save the store from their own daemon threads;
        # without this a background stamp and a UI "save as new" could interleave
        # so one save clobbers the other. Re-entrant so _mutate_store -> _persist
        # on the same thread does not deadlock. See _mutate_store / _persist.
        self._store_lock = threading.RLock()
        self.presets, self.store_extra = load_store(self.store_path)
        # saved_configs.json mtime at load, so _mutate_store can detect another process
        # (the headless one-click shortcut) writing the store and merge its change
        # in before re-applying ours (cross-process lost-update guard, item 11).
        self._store_mtime = self._store_mtime_now()
        # Resolve WHICH database is the lab-shared default from the store's
        # default_database reference, so core.LAB_DB (what every DB_MODE_LAB launch
        # uses) points at the chosen database -- the setup-wizard DB by default, or
        # a custom DB promoted in Lab Settings. Then re-pull the derived defaults.
        core.apply_default_database(self.store_extra)
        refresh_defaults_from_core()
        # Lab presets (Feature 4): the lab selector reads these instead of the
        # hardcoded tiles. Seeded from the constants for a fresh store, so the
        # two built-in labs are always present and old configs still resolve.
        self.lab_presets = core.lab_presets_from_store(self.store_extra)
        # This computer's lab (machine.json home_lab) configures the built-in
        # default's lab AND its room (from that lab's default_room). Applied in
        # memory to the built-in only; user configs untouched. A no-op on first
        # launch (marker unset).
        apply_lab_marker(self.presets, lab_presets=self.lab_presets)
        # The built-in Lab default is a launch TEMPLATE: force its last_run to
        # None on load so a stamp a previous version wrote is cleared (Job 2).
        clear_builtin_last_run(self.presets)
        # ...and force its project folder blank so it always opens Browse-first
        # (the launcher opens selected on the built-in every start, Job 2).
        clear_builtin_project_path(self.presets)
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
        # GitHub Organisation Sync opt-in (default OFF), a lab setting persisted in
        # lab_info.json. Loaded before the cards are built so the GitHub Org. / Git
        # update buttons start in the right (hidden) state.
        github_sync = core.load_github_sync()
        self.github_sync_enabled = github_sync["enabled"]
        self.github_org = github_sync["org"]
        # The automatic "is this study behind its online copy?" check (any git
        # repository, whatever the GitHub setting). It
        # runs core.project_update_status (a git FETCH: downloads only, never
        # changes the files, never prompts) on a background thread once per
        # project / config selection; the stored result is _update_status for the
        # folder _update_status_path. ``_update_checker`` and ``_update_check_async``
        # are hooks: tests swap the checker and run it synchronously.
        self._update_checker = core.project_update_status
        self._update_check_async = True
        self._update_status = {}
        self._update_status_path = ""
        self._update_is_repo = False
        self._update_token = 0
        self._update_pending = None      # (token, path, status) left by the worker
        self._update_thread = None
        self._git_pull_running = False

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
        # Always open SELECTED on the built-in Lab default -- a fresh,
        # Browse-first template every start (Job 2). Shared with the web face
        # through core.select_on_open, so both open on the same config.
        if self.presets:
            open_on = core.select_on_open(self.presets)
            open_index = self._index_of(open_on) if open_on is not None else 0
            self.select_preset(open_index if open_index is not None else 0, log_it=False)
        else:
            self.load_fields(DEFAULT_CONFIG, None)

        self.log("%s ready. Pick a config on the left, then click Launch." % APP_NAME, "muted")
        root.bind("<Control-Return>", lambda _e: self.launch())
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        # (Which lab this computer is, is asked by the setup wizard in main();
        # the old first-launch chooser is gone.)

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
        Tooltip(self.settings_button, self._settings_tooltip).attach(
            self.settings_button)
        # A newer launcher version is known: a small dot next to the gear (the
        # details and the Update button are in Lab Settings). core asks GitHub
        # at most once a day, in the background, a moment after start.
        self.update_dot = tk.Label(title, text="\u25cf", bg=COLORS["sidebar"],
                                   fg=COLORS["accent"], font=self.fonts.small,
                                   cursor="hand2")
        self.update_dot.bind("<Button-1>", lambda _e: self.open_lab_settings())
        self._launcher_update = {}
        self._update_badge_checker = core.check_for_update
        try:
            self.root.after(1500, self._start_update_badge)
        except (tk.TclError, RuntimeError):
            pass

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
        self.list_area.grid(row=2, column=0, sticky="nsew", padx=(PAD, 4), pady=(0, 2))

        self._build_sidebar_footer(bar)

    def _build_sidebar_footer(self, bar):
        """The WHOLE-APP footer (review I, Julian's final design): pinned at the
        BOTTOM of the left config sidebar, HORIZONTALLY CENTRED, always visible.
        IDENTITY ONLY -- no update tag (the update nudge lives on the Lab Settings
        page). THREE lines with tight/compressed spacing (especially close between
        line 1 and line 2). The lines come from core.version_footer_lines() so the
        two faces read identically. Muted/grey.

        (A soft fade of the overflowing config list into this footer is a web
        nice-to-have; here the must is the pinned, centred, three-line footer.)"""
        line1, line2, line3 = core.version_footer_lines()
        foot = tk.Frame(bar, bg=COLORS["sidebar"])
        foot.grid(row=3, column=0, sticky="ew", pady=(6, PAD))
        foot.columnconfigure(0, weight=1)
        # anchor="center" + a stretched column keeps the block centred; the two
        # top lines are packed flush (pady 0) so line 1 and line 2 sit tight.
        tk.Label(foot, text=line1, bg=COLORS["sidebar"], fg=COLORS["faint"],
                 font=self.fonts.small).grid(row=0, column=0, pady=(0, 0))
        tk.Label(foot, text=line2, bg=COLORS["sidebar"], fg=COLORS["faint"],
                 font=self.fonts.small).grid(row=1, column=0, pady=(0, 0))
        tk.Label(foot, text=line3, bg=COLORS["sidebar"], fg=COLORS["faint"],
                 font=self.fonts.small).grid(row=2, column=0, pady=(1, 0))

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

            # The built-in Lab default is a launch TEMPLATE, never a saved config,
            # so it NEVER shows a run time (Job 2) -- only researcher configs do.
            when = None
            if not is_builtin(preset):
                when = tk.Label(text, text=format_last_run(preset.get("last_run")), bg=bg,
                                fg=COLORS["muted"], font=self.fonts.small, anchor="w")
                when.pack(fill="x")

            widgets = ([row, accent, text, title, name]
                       + ([when] if when else [])
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
        title_row = tk.Frame(header, bg=COLORS["window"])
        title_row.pack(side="top", fill="x")
        self.subtitle = tk.Label(
            title_row, text="", bg=COLORS["window"], fg=COLORS["text"],
            font=self.fonts.heading, anchor="w")
        self.subtitle.pack(side="left", anchor="w")

        # The Save-as-new affordance: just the button, at the right of the title,
        # shown only while the setup is worth saving (_apply_save_affordance). No
        # sentence beside it (UX simplification): the button says what it does.
        self.banner = ttk.Button(title_row, text="Save as new config...",
                                 style="Slim.TButton", command=self.save_as_new)
        # "Save one-click shortcut": ALWAYS visible on the config page, the same
        # size and style as "Save as new config..." and right next to it (Julian,
        # 2026-10-01: people do it once but must recognise it). Hovering it says
        # what it gives you (core.ui_tip("one_click_shortcut")).
        self.shortcut_button = ttk.Button(
            title_row, text="Save one-click shortcut", style="Slim.TButton",
            command=self.save_shortcut)
        self.shortcut_button.pack(side="right", padx=(8, 0))
        self.shortcut_tooltip = Tooltip(
            self.shortcut_button, lambda: core.ui_tip("one_click_shortcut"), delay=250)
        self.shortcut_tooltip.attach(self.shortcut_button)

        # One-time notices (e.g. "Settings upgraded ...") sit in a slim info bar
        # under the header (show_notices); hidden until there is one.
        self.notice_bar = tk.Frame(header, bg=COLORS["accent_soft"],
                                   highlightbackground=COLORS["card_line"],
                                   highlightthickness=1)
        self.notice_bar.columnconfigure(0, weight=1)
        self.notice_label = tk.Label(
            self.notice_bar, text="", bg=COLORS["accent_soft"], fg=COLORS["text"],
            font=self.fonts.small_bold, anchor="w", justify="left")
        self.notice_label.grid(row=0, column=0, sticky="ew", padx=10, pady=4)
        close = tk.Label(self.notice_bar, text="×", bg=COLORS["accent_soft"],
                         fg=COLORS["muted"], font=self.fonts.bold, cursor="hand2")
        close.grid(row=0, column=1, padx=(6, 8))
        close.bind("<Button-1>", lambda _e: self.hide_notices())
        self.notice_bar.bind("<Configure>", lambda e: self.notice_label.configure(
            wraplength=max(e.width - 60, 220)))
        self._main_frame = main

        settings = ScrollFrame(main, COLORS["window"])
        settings.grid(row=2, column=0, sticky="nsew", padx=PAD + 4, pady=(4, 0))
        self.settings_pane = settings
        self._build_cards(settings.inner)

        self._build_bottom(main)          # row 3: the Export/Launch button bar

        log_frame = self._build_log(main)
        log_frame.grid(row=4, column=0, sticky="ew", padx=PAD + 4, pady=(0, PAD - 2))

    def show_notices(self, texts, warning=False):
        """Show one-time notices (core.take_notices) in the info bar under the
        header, bold, with a close ×. Nothing to show = nothing happens."""
        texts = [t for t in (texts or []) if t]
        if not texts:
            return False
        current = self.notice_label.cget("text")
        text = "\n".join(([current] if current else []) + texts)
        bg = COLORS["warn_soft"] if warning else COLORS["accent_soft"]
        self.notice_bar.configure(bg=bg)
        for child in self.notice_bar.winfo_children():
            child.configure(bg=bg)
        self.notice_label.configure(text=text)
        self.notice_bar.pack(side="bottom", fill="x", pady=(6, 0))
        return True

    def hide_notices(self):
        self.notice_label.configure(text="")
        self.notice_bar.pack_forget()

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
        # The GitHub button (shown when an organisation is set in Lab Settings >
        # GitHub) gets a study from that organisation; it carries the
        # organisation's name, like the web face's button.
        self.github_org_btn = ttk.Button(
            picker, text=self._github_button_text(), command=self.github_org_clone)
        self.github_org_btn.grid(row=0, column=2, padx=(8, 0))

        # The validation summary line (dot + one sentence)... with the standing
        # Git Pull button at its right (inside the status area, as on the web
        # face; it used to sit in the Browse row, where it squeezed the path).
        # It pulls the selected STUDY folder, never the launcher's own folder.
        self.project_status = StatusLine(body, self.fonts.small)
        self.project_status.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.git_update_btn = ttk.Button(
            body, text=core.GIT_PULL_BUTTON_LABEL, style="Slim.TButton",
            command=self.git_update_study)
        self.git_update_btn.grid(row=1, column=1, sticky="ne", padx=(8, 0), pady=(8, 0))
        self._apply_github_sync_buttons()

        # ...and, when the folder is a real oTree project, the app packages as a
        # title-cased bullet list beneath it.
        self.app_bullets = tk.Frame(body, bg=COLORS["card"])
        self.app_bullets.grid(row=2, column=0, columnspan=2, sticky="ew", padx=(20, 0))
        self.app_bullets.grid_remove()

        # The Git Pull RESULT section (hidden until a pull runs), then a thin
        # divider, then the merged "oTree admin" sub-section. They share row 3 so
        # the result sits at the bottom of the project status, above the divider.
        holder = tk.Frame(body, bg=COLORS["card"])
        holder.grid(row=3, column=0, columnspan=2, sticky="ew")
        # The automatic update check's line / banner (see _render_update_status):
        # hidden unless the check has something to say. It sits above the Git
        # Pull result, which still says what a pull brought in.
        self.update_box = tk.Frame(holder, bg=COLORS["card"])
        self.git_result = tk.Frame(holder, bg=COLORS["card"])
        self._git_result_path = ""
        self._git_divider = tk.Frame(holder, bg=COLORS["card_line"], height=1)
        self._git_divider.pack(fill="x", pady=(10, 8))

        # The admin row is ONE summary line (UX simplification, #9):
        #
        #     oTree admin   admin · STUDY · production · auto login   ✎
        #
        # Username, authentication level, production/debug and auto/manual login
        # are set once per study, so the main screen shows core.admin_summary and
        # the pen opens a panel with all of them (username, password, the
        # authentication level, Auto login, Production mode). The tk variables are
        # unchanged (var[...] / self.auto_login), so form_values / load_fields work
        # exactly as before.
        card_bg = COLORS["card"]
        header = tk.Frame(body, bg=card_bg)
        header.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        self.admin_sentence = header
        tk.Label(header, text="oTree admin", bg=card_bg, fg=COLORS["muted"],
                 font=self.fonts.small, anchor="w").pack(side="left")
        self.admin_summary = tk.StringVar(self.root, value="")
        self.admin_summary_label = tk.Label(
            header, textvariable=self.admin_summary, bg=card_bg, fg=COLORS["text"],
            font=self.fonts.small_bold, anchor="w")
        self.admin_summary_label.pack(side="left", padx=(8, 0))
        self.admin_edit_link = tk.Label(header, text=" ✎", bg=card_bg,
                                        fg=COLORS["accent"], font=self.fonts.small_bold,
                                        cursor="hand2")
        self.admin_edit_link.pack(side="left", padx=(4, 0))
        self.admin_edit_link.bind("<Button-1>", lambda _e: self._toggle_admin_edit())
        Tooltip(self.admin_edit_link,
                lambda: "Edit the oTree admin login, authentication level, auto login "
                        "and production mode").attach(self.admin_edit_link)

        # Auto login is a first-class config field (var["auto_login"]), so it
        # round-trips through save/load and marks the config dirty like any other.
        self.auto_login = self.var["auto_login"]

        # The panel behind the pen. It sits directly in the card body (row 5), NOT
        # in a wrapper frame: a Tk frame whose last child is grid_remove()d keeps
        # its old height, which would leave a blank gap after collapsing.
        self.admin_creds = tk.Frame(body, bg=card_bg)
        self.admin_creds.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        self.admin_creds.columnconfigure(1, weight=1)
        self._field(self.admin_creds, 0, "Admin username",
                    ttk.Entry(self.admin_creds, textvariable=self.var["admin_username"]))
        self.admin_password_entry = self._password_row(
            self.admin_creds, 1, "Admin password", self.var["admin_password"],
            self.show_admin_password)
        levels = tk.Frame(self.admin_creds, bg=card_bg)
        self.auth_radios = []
        for level in AUTH_LEVELS:
            radio = tk.Radiobutton(
                levels, text=level, value=level, variable=self.var["auth_level"],
                bg=card_bg, fg=COLORS["text"], activebackground=card_bg,
                activeforeground=COLORS["text"], selectcolor=card_bg,
                font=self.fonts.small, bd=0, highlightthickness=0, padx=0,
                cursor="hand2")
            radio.pack(side="left", padx=(0, 16))
            self.auth_radios.append(radio)
        self._field(self.admin_creds, 2, "Authentication level", levels)
        checks = tk.Frame(self.admin_creds, bg=card_bg)
        checks.grid(row=3, column=0, columnspan=2, sticky="w", pady=(2, 4))
        self.auto_login_check = tk.Checkbutton(
            checks, text="Auto login", variable=self.auto_login,
            bg=card_bg, fg=COLORS["text"], activebackground=card_bg,
            activeforeground=COLORS["text"], selectcolor=COLORS["accent"],
            font=self.fonts.small, anchor="w", bd=0, highlightthickness=0,
            padx=0, cursor="hand2")
        self.auto_login_check.pack(side="left")
        self.production_check = tk.Checkbutton(
            checks, text="Production mode", variable=self.var["production"],
            bg=card_bg, fg=COLORS["text"], activebackground=card_bg,
            activeforeground=COLORS["text"], selectcolor=COLORS["accent"],
            font=self.fonts.small, anchor="w", bd=0, highlightthickness=0,
            padx=0, cursor="hand2")
        self.production_check.pack(side="left", padx=(18, 0))
        info_tip(checks, PRODUCTION_TIP, font=self.fonts.small).pack(side="left")
        # Collapsed by default; the summary line states the values.
        self._admin_shown = False
        self._set_admin_edit(False)

        # (The "Reset the database before starting" checkbox now lives in the
        # Database card, next to the database it resets.)

        body.bind("<Configure>",
                  lambda e: self._wrap_project_status(e.width))

    def _set_admin_edit(self, shown):
        """Show/hide the oTree admin panel (the pen): username, password,
        authentication level, auto login, production mode."""
        self._admin_shown = bool(shown)
        if not hasattr(self, "admin_creds"):
            return
        if self._admin_shown:
            self.admin_creds.grid()
        else:
            self.admin_creds.grid_remove()

    def _toggle_admin_edit(self):
        self._set_admin_edit(not getattr(self, "_admin_shown", False))

    def _apply_admin_visibility(self):
        """Collapse the oTree admin panel when a config loads: the one-line
        summary (core.admin_summary) already states the username, level,
        production and auto login, so nothing unusual is hidden."""
        self._set_admin_edit(False)

    def _refresh_admin_summary(self, cfg=None):
        if hasattr(self, "admin_summary"):
            self.admin_summary.set(core.admin_summary(cfg or self.form_values()))

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
        # "on HOST:PORT" in grey when the database is on another computer.
        self.db_host_note = tk.StringVar(self.root, value="")
        # Warn: the config's database is missing on this PC, or a localhost
        # database made on another PC. (Where/when it was created is shown in the
        # database's Edit dialog only.)
        self.db_warning = tk.StringVar(self.root, value="")

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
        tk.Label(left, textvariable=self.db_host_note, bg=COLORS["card"],
                 fg=COLORS["faint"], font=self.fonts.small, anchor="w").pack(
            side="left", padx=(6, 0))
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

        # Under the name, only when needed: a warning.
        # The location warning is the SHORT core.database_location_note; the full
        # sentence sits behind the info tip next to it (and in the Edit dialog).
        self.db_warning_row = tk.Frame(body, bg=COLORS["card"])
        self.db_warning_row.grid(row=2, column=0, columnspan=2, sticky="w")
        self.db_warning_label = tk.Label(
            self.db_warning_row, textvariable=self.db_warning, bg=COLORS["card"],
            fg=COLORS["warn"], font=self.fonts.small, anchor="w", justify="left",
            wraplength=420)
        self.db_warning_label.pack(side="left")
        self._db_location_full = ""
        self.db_warning_tip = info_tip(self.db_warning_row,
                                       lambda: self._db_location_full,
                                       font=self.fonts.small)

        # A "Details" disclosure (Feature 3): the selected database (the default OR
        # custom) shows only the one-line summary above; Details reveals the
        # connection READ-ONLY. All editing now happens in Lab Settings -> Database,
        # so the run/config screen never edits a connection inline.
        self.db_details_shown = tk.BooleanVar(self.root, value=False)
        self.db_details_toggle = tk.Label(
            body, text="Details ▸", bg=COLORS["card"], fg=COLORS["accent"],
            font=self.fonts.small_bold, anchor="w", cursor="hand2")
        self.db_details_toggle.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.db_details_toggle.bind("<Button-1>", self._toggle_db_details)

        # The connection fields, revealed READ-ONLY by the Details disclosure.
        # apply_db_mode keeps them read-only; _apply_db_details_view grids them.
        self.db_section = tk.Frame(body, bg=COLORS["card"])
        self.db_section.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))
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

        # Start on the one-line summary with Details collapsed; the disclosure
        # reveals the read-only connection on demand for any database.
        self.db_section.grid_remove()
        self._refresh_db_summary()
        self._apply_db_details_view()

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
            if value == LAB_LOCAL:
                # What "localhost" is for: behind the info tip, not a grey note.
                self.local_tip = info_tip(self.host_links, core.ui_tip("localhost_run"),
                                          font=self.fonts.small)
                self.local_tip.pack(side="left", padx=(0, 8))
        self.back_to_lab_link = tk.Label(
            self.host_links, text="← Back to a lab", bg=COLORS["card"], fg=COLORS["muted"],
            font=self.fonts.small, anchor="w", cursor="hand2")
        self.back_to_lab_link.bind(
            "<Button-1>",
            lambda _e: self.var["lab"].set(core.default_selected_lab(self.lab_presets)))

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
        # The explanation is behind the info tip next to Browse; this line only
        # appears when it has something to ASK for (no file found: use Browse).
        self.seat_file_note = tk.Label(
            self.seat_file_row, text="",
            bg=COLORS["card"], fg=COLORS["faint"], font=self.fonts.small,
            anchor="w", justify="left", wraplength=360)
        self.seat_file_note.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(6, 4))
        self.seat_file_note.grid_remove()
        self.seat_file_note.bind(
            "<Configure>", lambda e: self.seat_file_note.configure(wraplength=max(e.width - 4, 200)))
        # Detected candidate files in the project, plus a Browse fallback.
        self._candidate_file_map = {}
        self.seat_file_choice = tk.StringVar(self.root, value="")
        self.seat_file_combo = ttk.Combobox(
            self.seat_file_row, textvariable=self.seat_file_choice, state="readonly")
        self.seat_file_combo.grid(row=1, column=0, sticky="ew")
        self.seat_file_combo.bind("<<ComboboxSelected>>", self._on_candidate_file_pick)
        ttk.Button(self.seat_file_row, text="Browse...",
                   command=self.browse_seat_file).grid(row=1, column=1, padx=(8, 0))
        self.seat_file_tip = info_tip(self.seat_file_row, core.ui_tip("seat_file"),
                                      font=self.fonts.small)
        self.seat_file_tip.grid(row=1, column=2, padx=(4, 0))
        self.seat_file_box = PathBox(self.seat_file_row, self.fonts.body,
                                     self.var["seat_file"], placeholder="No file chosen yet")
        self.seat_file_box.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(6, 2), ipady=1)

        # -- top-down room-layout seat map. The caption is a toggle. Its DEFAULT
        #    follows the lab (Julian, 2026-10-01; core.lab_has_drawn_map): EXPANDED
        #    when the lab has a real drawn layout, COLLAPSED when it is only an
        #    ordered list of seat numbers. Always open in Edit mode, where the
        #    seats themselves are the selector. The default is applied once per
        #    lab (_map_default_for); after that the toggle is the user's.
        self._map_expanded = False
        self._map_default_for = None
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
            self.block_actions, text=core.GET_READY_LABEL, command=self.add_block,
            font=self.fonts.small_bold, bg=COLORS["accent"], fg="#ffffff",
            activebackground=COLORS["accent_dark"], activeforeground="#ffffff",
            relief="flat", padx=12, pady=5, cursor="hand2")
        self.block_button.grid(row=0, column=0, sticky="w")
        body.bind("<Configure>", self._resize_lab_labels)

    def _rebuild_lab_tiles(self, ncols=2, config_lab=None):
        """(Re)build one tile per lab the selector should offer. Called at
        start-up, after the Lab Settings page changes which labs are shown, and
        on every config load (with that config's own lab).

        The offered labs are the DISPLAYED ones PLUS the loaded config's own lab
        even when it is hidden (core.lab_options_for_config), so a config saved
        on a now-hidden lab keeps a tile for it and does not get silently
        reassigned (BUG B). If exactly one lab is offered it becomes the forced
        default so a researcher on a single-lab machine cannot pick the wrong
        lab."""
        frame = self.lab_tiles_frame
        for parts in self.lab_tiles.values():
            parts["frame"].destroy()
        self.lab_tiles = {}
        if getattr(self, "_single_lab_frame", None) is not None:
            self._single_lab_frame.destroy()
            self._single_lab_frame = None
        for c in range(16):
            frame.columnconfigure(c, weight=0, uniform="")
        presets = core.lab_options_for_config(self.lab_presets, config_lab)
        if not presets:
            # Defence against a hand-edited store that hid every lab (and no
            # config lab to include): show them all rather than an empty board.
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
        # Force the single-lab default, else keep the current selection when it
        # is among the offered labs (incl. the config's own hidden lab), else
        # fall to the first. A pseudo-host (custom / local) selection is left
        # untouched -- it is not a lab tile.
        if getattr(self, "var", None) is not None:
            current = self.var["lab"].get()
            ids = [p["id"] for p in presets]
            if current in (LAB_CUSTOM, LAB_LOCAL):
                forced = current
            elif single:
                forced = ids[0]
            elif current in ids:
                forced = current
            else:
                forced = ids[0] if ids else current
            if forced and current != forced:
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
        else:
            self.custom_fields.grid_remove()
            self.room_row.grid()

        # The room-layout map: collapsible in Lab-default mode (collapsed by
        # default), auto-opened in Edit mode where the seats are the selector.
        if not pseudo and mode in (SEAT_DEFAULT, SEAT_EDIT):
            interactive = (mode == SEAT_EDIT)
            if getattr(self, "_map_default_for", None) != lab:
                self._map_default_for = lab
                self._map_expanded = self._lab_has_drawn_map(lab)
            expanded = interactive or self._map_expanded
            default_seats = self._lab_default_seats(self.form_values())
            total = len(default_seats)
            arrow = "▾" if expanded else "▸"   # ▾ / ▸
            if interactive:
                # The caption counts what is left ("29 of 31 seats"); the one line
                # under it is the instruction.
                excluded = len(self.seat_excluded & set(default_seats))
                self.map_caption.configure(
                    text="%s  %s" % (arrow, core.seats_caption(total, excluded)))
                self.map_subcaption.configure(text="Click a seat to include or exclude it")
            else:
                self.map_caption.configure(
                    text="%s  %s" % (arrow, core.seats_caption(total)))
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
        # Only an instruction is shown: nothing found -> use Browse.
        if values:
            self.seat_file_note.grid_remove()
        else:
            self.seat_file_note.configure(
                text="No .txt files found in your project. Use Browse to pick one.")
            self.seat_file_note.grid()

    def apply_seat_mode(self):
        """Show the file picker for File mode; every other mode uses the map."""
        mode = self.var["seat_mode"].get()
        self.seat_file_row.grid_remove()
        # The holder itself is hidden too: an emptied Tk frame keeps its old
        # height, which left a blank gap in the Lab card after File mode.
        self.seat_extra.grid_remove()
        if mode == SEAT_FILE:
            self._refresh_candidate_files()
            self.seat_extra.grid()
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

        holder = self.log_holder = tk.Frame(frame, bg=COLORS["log_bg"], highlightthickness=1,
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
        self.log_text.tag_configure("flash", background="#34465e")
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
        self.inline_status.on_show_log = self.reveal_activity_log

        # Launch is the ONE action in this bar. "Save one-click shortcut" lives in
        # the header, next to "Save as new config..." (see _build_main).
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

    def reveal_activity_log(self):
        """The "See activity log" button: expand the activity log panel, scroll it
        to the latest lines and briefly highlight it (border + the last lines)."""
        try:
            self.root.deiconify()
            self.root.lift()
        except tk.TclError:
            pass
        self._log_expanded = True
        try:
            self.log_text.configure(height=12)
            self.log_caption.configure(text="ACTIVITY LOG  ▾")
            self.log_text.see("end")
            self.log_text.yview_moveto(1.0)
            self.log_text.tag_remove("flash", "1.0", "end")
            self.log_text.tag_add("flash", "end-4l linestart", "end")
            self.log_holder.configure(highlightbackground=COLORS["accent"],
                                      highlightcolor=COLORS["accent"], highlightthickness=2)
        except tk.TclError:
            return

        def _unflash():
            try:
                self.log_text.tag_remove("flash", "1.0", "end")
                self.log_holder.configure(highlightbackground=COLORS["card_line"],
                                          highlightcolor=COLORS["card_line"],
                                          highlightthickness=1)
            except tk.TclError:
                pass
        self.root.after(1600, _unflash)

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

    def _lab_has_drawn_map(self, lab):
        """core.lab_has_drawn_map for a lab id: a real drawn layout (expanded by
        default) versus a plain list of seat numbers (collapsed by default)."""
        preset = core.find_lab_preset(lab, self.lab_presets)
        if preset is None:
            return False
        if core.lab_has_drawn_map(preset):
            return True
        # Same fallback as _lab_seatmap_data: a stored preset without a resolved
        # map object takes the map its lab defines in lab_info.json.
        info_preset = core.find_lab_preset(lab, core.default_lab_presets())
        return bool(info_preset is not None and core.lab_has_drawn_map(
            dict(info_preset, seats=preset.get("seats", []))))

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
            # A lab preset without a resolved map object (e.g. one just added) has no map on the stored
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
            # A config saved with the lab's default room follows the lab's
            # CURRENT default room: the screen shows what will launch.
            values = normalize_config(core.follow_lab_room(cfg, self.lab_presets))
            for key in FIELD_KEYS:
                value = values[key]
                self.var[key].set(json.dumps(value) if isinstance(value, list) else value)
            # Rebuild the lab selector to OFFER this config's OWN lab -- even when
            # it is hidden (display=False) -- so opening a config saved on a
            # now-hidden lab neither drops nor silently reassigns its lab (BUG B).
            # The rebuild keeps the config's lab selected when a preset carries it
            # and only falls back to a forced/first lab when the id truly does not
            # exist at all. var["lab"] is already set to values["lab"] above.
            self._rebuild_lab_tiles(config_lab=values["lab"])
            self.seat_excluded = set(values["seat_excluded"])
            self.db_mode_label.set(DB_MODE_LABELS[values["db_mode"]])
            self.seat_mode_label.set(SEAT_MODE_LABELS[values["seat_mode"]])
            self.show_db_password.set(False)
            self.show_admin_password.set(False)
            self._map_default_for = None   # re-apply the map default for this config's lab
            for entry, _flag in self._password_entries:
                entry.configure(show=MASK_CHAR)
        finally:
            self.loading = False
        self.selected_index = index
        self.dirty = False
        self.apply_db_mode()
        self.apply_seat_mode()
        self._apply_lab_view()
        self._apply_admin_visibility()   # collapse the admin panel
        self.refresh_previews()
        self.refresh_dirty()
        self.refresh_sidebar()

    def select_preset(self, index, log_it=True):
        if not (0 <= index < len(self.presets)):
            return
        preset = self.presets[index]
        checked_path = self._update_status_path
        self.load_fields(preset, index)
        # A config selected (again) on the SAME study folder is a new selection:
        # look at GitHub again (a new folder was already checked by load_fields).
        if checked_path and checked_path == self._update_status_path:
            self._sync_update_check(checked_path, reselect=True)
        self.inline_status.set("muted", "")
        if log_it:
            self.log('Loaded config "%s".' % preset.get("name", ""), "info")

    def _on_field_change(self, *_args):
        if self.loading:
            return
        self.refresh_previews()
        self.refresh_dirty()

    def _open_db_menu(self, event=None):
        """The pen icon opens the database picker: the oTree default (SQLite)
        and every database of this computer (each with its creator in grey and
        where it was created), plus "Add new database…".
        The list is core.list_databases so both launchers show the same picker."""
        DatabasePickerDialog(self.root, self.fonts, self)

    def current_database_id(self):
        """The id of the database the on-screen config uses (for the picker to
        tick): core.current_database_id decides."""
        return core.current_database_id(self.form_values())

    def choose_database(self, entry):
        """Apply a picked database entry to the on-screen config (SQLite or one
        of this PC's databases). core.database_config_fields sets the saved
        reference (database_id) plus the derived db_mode + connection."""
        fields = core.database_config_fields(entry)
        for key, value in fields.items():
            if key in self.var:
                self.var[key].set(value)
        self.db_mode_label.set(DB_MODE_LABELS[fields["db_mode"]])
        self.apply_db_mode()
        self.refresh_previews()
        self.log("Database set to %s." % entry.get("title", "the chosen database"), "info")

    def _choose_db_mode(self, mode):
        """Switch the database from the pen menu. "lab" now means this
        computer's default database (core.switch_to_lab_default pins it)."""
        if mode == DB_MODE_LAB and not self.loading:
            fields = core.switch_to_lab_default(self.form_values())
            for key in ("database_id",) + core.CONFIG_DB_FIELDS:
                if key in self.var:
                    self.var[key].set(fields[key])
            mode = fields["db_mode"]
        elif mode == DB_MODE_NONE:
            self.var["database_id"].set(core.DB_BUILTIN_SQLITE)
        self.var["db_mode"].set(mode)
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
            # Pin the room to THIS lab's own default_room, not a hardcoded "study",
            # so clicking a lab whose default room differs sets the right room and
            # keeps the "(lab default)" tag (fable review item 5).
            room = core.lab_default_room(self.lab_presets, lab)
            if self.var["room_name"].get().strip() != room:
                self.var["room_name"].set(room)
            if self.var["seat_mode"].get() == SEAT_NONE:
                self.seat_mode_label.set(SEAT_MODE_LABELS[SEAT_DEFAULT])
        elif lab == LAB_CUSTOM:
            self.seat_mode_label.set(SEAT_MODE_LABELS[SEAT_NONE])
        else:  # LAB_LOCAL: keep the room at the lab default, but no seat board
            room = core.lab_default_room(self.lab_presets, lab)
            if self.var["room_name"].get().strip() != room:
                self.var["room_name"].set(room)
            self.seat_mode_label.set(SEAT_MODE_LABELS[SEAT_NONE])
        self._apply_lab_view()

    def apply_db_mode(self):
        mode = self.var["db_mode"].get()
        # Feature 3: every database (this PC's databases, oTree default) shows only
        # a one-line summary on the run/config screen; the connection fields are
        # READ-ONLY behind the Details disclosure. Editing a database has moved
        # entirely to Lab Settings -> Database, so nothing is editable inline here.
        for entry in self.db_entries.values():
            entry.configure(state="readonly")
        if mode != DB_MODE_CUSTOM:
            self.set_password_visible("db", False)
        self._refresh_db_summary()
        self._apply_db_details_view()

    def _toggle_db_details(self, _event=None):
        self.db_details_shown.set(not self.db_details_shown.get())
        self._apply_db_details_view()

    def _apply_db_details_view(self):
        """Grid or hide the read-only connection block for the Details disclosure."""
        shown = getattr(self, "db_details_shown", None)
        toggle = getattr(self, "db_details_toggle", None)
        if shown is None or toggle is None:
            return
        if shown.get():
            self.db_section.grid()
            toggle.configure(text="Details ▾")
        else:
            self.db_section.grid_remove()
            toggle.configure(text="Details ▸")

    def _db_db_fields(self):
        return {key: self.var[key].get() for key in
                ("database_id", "db_mode", "db_name", "db_user", "db_host", "db_port")}

    def _db_summary_text(self):
        # The reset state is shown by the always-visible "Reset before start"
        # tickbox on the same line, so it is NOT repeated in this summary text.
        return core.database_summary_label(self._db_db_fields())

    def _resolved_db_entry(self):
        """This PC's database entry the on-screen config resolves to, or None."""
        return core.find_database(self.store_extra,
                                  core.current_database_id(self._db_db_fields()))

    def _refresh_db_summary(self):
        if hasattr(self, "db_summary"):
            self.db_summary.set(self._db_summary_text())
        if hasattr(self, "db_host_note"):
            note = core.database_host_note(self._db_db_fields())
            self.db_host_note.set(note)
        if hasattr(self, "db_warning"):
            entry = self._resolved_db_entry()
            self._db_location_full = (core.database_location_warning(entry)
                                      if entry else "")
            warnings = [core.config_database_note(self._db_db_fields()),
                        core.database_location_note(entry) if entry else ""]
            self.db_warning.set("\n".join(w for w in warnings if w))
            if self.db_warning.get():
                self.db_warning_row.grid()
            else:
                self.db_warning_row.grid_remove()
            if self._db_location_full:
                self.db_warning_tip.pack(side="left", anchor="s")
            else:
                self.db_warning_tip.pack_forget()

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
        self._refresh_admin_summary(cfg)
        self._refresh_run_summary(cfg)

        # Project validation: a one-line summary, plus the app packages as a
        # title-cased bullet list when the folder really is an oTree project.
        level, message = validate_project(cfg["project_path"])
        if level == "ok":
            apps = find_app_packages(cfg["project_path"])
            self.project_status.set("ok",
                "Looks like an oTree project with settings.py and %d app package%s:"
                % (len(apps), "" if len(apps) == 1 else "s"))
            self._set_app_bullets(apps)
        else:
            self.project_status.set(level, message)
            self._set_app_bullets([])
        self._sync_git_result(cfg["project_path"])
        self._sync_update_check(cfg["project_path"])
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
            self.map_caption.configure(
                text="▾  %s" % core.seats_caption(total, total - chosen))
            self.map_subcaption.configure(text="Click a seat to include or exclude it")

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
        self.dirty = configs_differ(self.form_values(),
                                    core.follow_lab_room(preset, self.lab_presets),
                                    ignore=ignore)
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
        """Show the 'Save as new config...' button (right of the title) when the
        setup is worth saving, and hide it otherwise."""
        if not hasattr(self, "banner"):
            return
        if self._needs_save():
            # Rightmost, with "Save one-click shortcut" (always there) directly to
            # its left: the same order as the web face.
            self.banner.pack(side="right", padx=(8, 0), before=self.shortcut_button)
        else:
            self.banner.pack_forget()

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
        level, message = validate_project(chosen)
        self.log("Project folder: %s" % chosen, "info")
        self.log(message, {"ok": "ok", "warn": "warn", "error": "err"}[level])
        # Right after a fresh selection, offer the one-click lab setup if the
        # project is not ready. Shown at most once, here, per selection.
        if offer_get_ready:
            self._maybe_offer_get_ready(path)
        return path

    # -- GitHub -------------------------------------------------------------
    #
    # ONE setting (Lab Settings > GitHub): the organisation name. A name shows
    # the GitHub button (clone from that organisation); an empty name hides it.
    # The update check and Git Pull do NOT depend on it: they work for any study
    # folder that is a git repository. Everything here reuses the shared core git
    # plumbing and NEVER touches the launcher app/ folder or any oTree process.

    def _wrap_project_status(self, width=None):
        """Wrap the project status line inside the room it really has: the card
        width minus the standing Git Pull button when that is shown beside it
        (otherwise the line runs under the button and squeezes it)."""
        try:
            if width is None:
                width = self.project_status.master.winfo_width()
            room = int(width) - 24
            button = getattr(self, "git_update_btn", None)
            if button is not None and button.winfo_manager():
                room -= button.winfo_reqwidth() + 12
            self.project_status.set_wraplength(room)
        except (tk.TclError, AttributeError, ValueError):
            pass

    def _github_button_text(self):
        org = str(getattr(self, "github_org", "") or "").strip()
        return ("GitHub: %s" % org) if org else "GitHub..."

    def _clone_enabled(self):
        return core.github_clone_enabled({"enabled": getattr(self, "github_sync_enabled", False),
                                          "org": getattr(self, "github_org", "")})

    def _apply_github_sync_buttons(self):
        """Show the GitHub button only when an organisation is set, and the
        standing Git Pull button only when the chosen study folder is a git
        repository (a plain folder has nothing to pull) AND the amber "newer
        version" banner is not showing its own Update button (one button at a
        time). Safe to call before/after the buttons exist."""
        banner = (getattr(self, "_update_status", None) or {}).get("state") == \
            core.UPDATE_STATE_BEHIND
        shown = {"github_org_btn": self._clone_enabled(),
                 "git_update_btn": bool(getattr(self, "_update_is_repo", False))
                 and not banner}
        for name, show in shown.items():
            btn = getattr(self, name, None)
            if btn is None:
                continue
            if show:
                btn.grid()
            else:
                btn.grid_remove()
        btn = getattr(self, "github_org_btn", None)
        if btn is not None and str(btn.cget("state")) != "disabled":
            btn.config(text=self._github_button_text())
        if getattr(self, "project_status", None) is not None:
            self._wrap_project_status()

    def _set_github_sync(self, enabled, org):
        """Persist the on/off flag + org name (lab settings, lab_info.json) and
        refresh the buttons. Fail-soft in core; returns the stored state."""
        stored = core.save_github_sync_settings(enabled, org)
        self.github_sync_enabled = stored["enabled"]
        self.github_org = stored["org"]
        self._apply_github_sync_buttons()
        return stored

    def _set_github_org(self, org):
        """The ONE GitHub setting: a name switches the GitHub button on for that
        organisation, an empty name switches it off (core.save_github_org)."""
        stored = core.save_github_org(org)
        self.github_sync_enabled = stored["enabled"]
        self.github_org = stored["org"]
        self._apply_github_sync_buttons()
        return stored

    # -- the launcher's own update: a dot on the gear -----------------------

    def _settings_tooltip(self):
        update = getattr(self, "_launcher_update", None) or {}
        if update.get("update_available"):
            remote = update.get("remote_version") or ""
            return "Lab Settings · a new version is available%s" % (
                (" (%s)" % remote) if remote else "")
        return "Lab Settings"

    def _start_update_badge(self):
        run_in_background(self.root, self._update_badge_checker, self._apply_update_badge)

    def _apply_update_badge(self, update):
        self._launcher_update = dict(update or {})
        dot = getattr(self, "update_dot", None)
        if dot is None:
            return
        try:
            if self._launcher_update.get("update_available"):
                dot.pack(side="right", before=self.settings_button)
            else:
                dot.pack_forget()
        except tk.TclError:
            pass

    # -- automatic update check (is the study folder behind its online copy?) --
    #
    # For any git repository. Quiet by design: "none" (not a repo, offline,
    # timeout) shows NOTHING, and the check can never block selecting a project,
    # typing or Launch: it runs on a thread, and its result is dropped when
    # another folder has been selected meanwhile.

    # How long after a selection the background check starts (ms).
    UPDATE_CHECK_DELAY_MS = 250

    def _current_project_path(self):
        try:
            return (self.var["project_path"].get() or "").strip()
        except (tk.TclError, KeyError, AttributeError):
            return ""

    def _clear_update_check(self):
        self._update_token += 1          # drops whatever a running check returns
        self._update_pending = None
        self._update_status = {}
        self._update_status_path = ""
        self._update_is_repo = False
        self._apply_github_sync_buttons()
        self._render_update_status()

    def _sync_update_check(self, project_path, reselect=False):
        """Keep the update check in step with the selected study folder: a NEW
        folder (or, with ``reselect``, a config selected again) starts ONE check;
        no folder clears it. Cheap when nothing changed, so every
        refresh_previews may call it."""
        path = (project_path or "").strip()
        if not path or not os.path.isdir(path):
            if self._update_status_path or self._update_status or self._update_is_repo:
                self._clear_update_check()
            return
        same = (path == self._update_status_path)
        if same and not reselect:
            return
        if not same:
            # Another folder: forget the old answer at once (nothing is shown
            # until the new check answers), and decide the Git Pull button with
            # the cheap is-it-a-repo test, without waiting for the fetch.
            self._update_status = {}
            self._update_status_path = path
            try:
                self._update_is_repo = bool(core.is_git_repo(path)) and \
                    not core._is_launcher_folder(path)
            except Exception:
                self._update_is_repo = False
            self._apply_github_sync_buttons()
            self._render_update_status()
        if self._update_is_repo:
            self._start_update_check(path)

    def _start_update_check(self, path):
        """Run the checker for ``path``: on a daemon thread (the main thread
        polls for the result, so no Tk call is ever made from the worker), or at
        once when ``_update_check_async`` is off (tests)."""
        self._update_token += 1
        token = self._update_token
        checker = self._update_checker
        if not self._update_check_async:
            try:
                status = checker(path)
            except Exception:
                status = {}
            self._apply_update_status(path, status, token)
            return

        def work():
            try:
                status = checker(path)
            except Exception:                 # the check must never surface an error
                status = {}
            self._update_pending = (token, path, status)

        def kick():
            # Started a moment AFTER the selection, from the event loop: clicking
            # through several configs starts one check (the last), not one each,
            # and it never runs at the same time as a Git Pull of this folder.
            if token != self._update_token or self._git_pull_running:
                return
            self._update_thread = threading.Thread(target=work, name="update-check",
                                                   daemon=True)
            self._update_thread.start()
            self._poll_update_check(token)

        try:
            self.root.after(self.UPDATE_CHECK_DELAY_MS, kick)
        except (tk.TclError, RuntimeError):
            pass

    def _poll_update_check(self, token, tries=0):
        if token != self._update_token:
            return                             # superseded by a newer selection
        pending = self._update_pending
        if pending is not None and pending[0] == token:
            self._update_pending = None
            self._apply_update_status(pending[1], pending[2], token)
            return
        if tries > 400:                        # ~60 s: well past the fetch timeout
            return
        try:
            self.root.after(150, lambda: self._poll_update_check(token, tries + 1))
        except (tk.TclError, RuntimeError):
            pass

    def _apply_update_status(self, path, status, token=None):
        """Main thread: store and show a check's result, unless it is for a
        folder that is no longer the selected one (a stale result is dropped)."""
        if token is not None and token != self._update_token:
            return
        if (path or "").strip() != self._current_project_path() \
                or (path or "").strip() != self._update_status_path:
            return
        was = (self._update_status or {}).get("state")
        self._update_status = dict(status or {})
        if self._update_status.get("is_repo"):
            self._update_is_repo = True
        self._apply_github_sync_buttons()
        self._render_update_status()
        # The pre-launch screen is open and a newer version just turned up (the
        # check repeated when Launch was pressed): show it there too.
        if self._update_status.get("state") == core.UPDATE_STATE_BEHIND \
                and was != core.UPDATE_STATE_BEHIND:
            dialog = getattr(self, "_briefing_dialog", None)
            try:
                if dialog is not None and dialog.top.winfo_exists() \
                        and not dialog._launching:
                    dialog._rerender()
            except (tk.TclError, AttributeError):
                pass

    def _recheck_update_if_stale(self, path):
        """Launch was pressed: repeat an update check whose answer is old (core
        decides what old is). In the background; the launch screen never waits."""
        path = (path or "").strip()
        if path and path == self._update_status_path and self._update_is_repo \
                and core.update_status_is_stale(self._update_status):
            self._start_update_check(path)

    def _render_update_status(self):
        """Draw the update check's result at the top of the Git area of the
        project status box:

          none           nothing
          current        one quiet muted line ("Experiment up to date · checked HH:MM")
                         (hidden while a successful Git Pull result says the same)
          behind         an amber banner with the question and an Update button
          local_changes  one line + "Get a fresh copy"
          no_access      one grey line + "GitHub login…"
        """
        box = getattr(self, "update_box", None)
        if box is None:
            return
        try:
            for child in list(box.winfo_children()):
                child.destroy()
        except tk.TclError:
            return
        self.update_pull_button = None
        self.update_message_label = None
        self.update_action_button = None
        status = self._update_status or {}
        state = status.get("state") or core.UPDATE_STATE_NONE
        message = str(status.get("message") or "")
        same = self._git_result_path == self._update_status_path
        pulled_ok = bool(getattr(self, "_git_result_ok", False) and same)
        # ...and a FAILED pull that offers the same way out replaces the line
        # that offers it too (one button, not two).
        offered = getattr(self, "_git_result_offers", ()) if same else ()
        if state == core.UPDATE_STATE_NONE or not message \
                or (state == core.UPDATE_STATE_CURRENT and pulled_ok) \
                or (state == core.UPDATE_STATE_LOCAL_CHANGES and "fresh_copy" in offered) \
                or (state == core.UPDATE_STATE_NO_ACCESS and "login" in offered):
            box.pack_forget()
            return
        bg = COLORS["card"]
        if state == core.UPDATE_STATE_BEHIND:
            banner = tk.Frame(box, bg=COLORS["warn_soft"], highlightthickness=1,
                              highlightbackground=COLORS["card_line"])
            banner.pack(fill="x")
            banner.columnconfigure(0, weight=1)
            self.update_message_label = tk.Label(
                banner, text=message, bg=COLORS["warn_soft"], fg=COLORS["warn"],
                font=self.fonts.small_bold, anchor="w", justify="left", wraplength=300)
            self.update_message_label.grid(row=0, column=0, sticky="ew", padx=(10, 8), pady=6)
            self.update_pull_button = ttk.Button(
                banner, text=status.get("action_label") or core.GIT_UPDATE_ACTION_LABEL,
                style="Slim.TButton", command=self.git_update_study)
            self.update_pull_button.grid(row=0, column=1, sticky="e", padx=(0, 8), pady=5)
            if self._git_pull_running:
                self.update_pull_button.state(["disabled"])
        else:
            warn = state == core.UPDATE_STATE_LOCAL_CHANGES
            row = tk.Frame(box, bg=bg)
            row.pack(fill="x")
            row.columnconfigure(0, weight=1)
            self.update_message_label = tk.Label(
                row, text=message, bg=bg, fg=COLORS["warn"] if warn else COLORS["faint"],
                font=self.fonts.small, anchor="w", justify="left", wraplength=300)
            self.update_message_label.grid(row=0, column=0, sticky="ew")
            action = status.get("action")
            if action == "fresh_copy":
                self.update_action_button = ttk.Button(
                    row, text=status.get("action_label") or core.GIT_FRESH_COPY_LABEL,
                    style="Slim.TButton", command=self.get_fresh_copy)
            elif action == "github_login":
                self.update_action_button = ttk.Button(
                    row, text=core.GITHUB_LOGIN_ACTION_LABEL, style="Slim.TButton",
                    command=lambda: self.open_github_login(self._recheck_after_login))
            if self.update_action_button is not None:
                self.update_action_button.grid(row=0, column=1, sticky="e", padx=(8, 0))
        before = self.git_result if self.git_result.winfo_manager() else self._git_divider
        box.pack(fill="x", pady=(8, 0), before=before)

    def open_github_login(self, on_saved=None, retry=False):
        """THE GitHub login dialog (GithubLoginDialog), from the main window: the
        "could not check" line and a failed Git Pull. ``on_saved()`` runs after a
        login was stored (retry what failed)."""
        def done(ok, message, saved=False):
            self.log(message, "ok" if ok else "err")
            if ok and saved and callable(on_saved):
                on_saved()
        GithubLoginDialog(self.root, self.fonts, on_done=done, retry=retry,
                          org=self.github_org)

    def _recheck_after_login(self):
        self._sync_update_check(self._current_project_path(), reselect=True)

    def get_fresh_copy(self):
        """"Get a fresh copy": clone the study's repository again into a NEW
        folder next to this one and select it (core.git_fresh_copy). The old
        folder is never changed: the launcher does not commit, merge or discard
        in a study folder."""
        path = self._current_project_path()
        if not path or getattr(self, "_fresh_copy_running", False):
            return False
        self._fresh_copy_running = True
        for name in ("update_action_button", "fresh_copy_button"):
            button = getattr(self, name, None)
            if button is not None:
                try:
                    button.state(["disabled"])
                except tk.TclError:
                    pass
        self.log("Getting a fresh copy of %s ..." % path, "info")
        self._render_git_result({"busy": True, "busy_text": "Getting a fresh copy..."}, path)
        if not self._update_check_async:                 # tests: run it at once
            self._on_fresh_copy_done(core.git_fresh_copy(path), path)
            return True
        run_in_background(self.root, lambda: core.git_fresh_copy(path),
                          lambda result: self._on_fresh_copy_done(result, path))
        return True

    def _on_fresh_copy_done(self, result, old_path):
        self._fresh_copy_running = False
        result = result or {"ok": False, "message": "Could not get a fresh copy."}
        if result.get("ok") and result.get("path"):
            new = os.path.normpath(result["path"])
            self._clear_git_result()
            self.var["project_path"].set(new)
            self.log(result.get("message") or ("Fresh copy saved in %s." % new), "ok")
            self.refresh_previews()
            self._maybe_offer_get_ready(new)
            return
        self.log(result.get("message") or "Could not get a fresh copy.", "err")
        if result.get("output"):
            self.log(result["output"], "info")
        self._render_git_result(
            {"ok": False, "title": result.get("message") or "Could not get a fresh copy.",
             "reason": result.get("hint", ""), "output": result.get("output", ""),
             "login_action": result.get("status") in ("auth", "not_found")}, old_path)
        self._render_update_status()

    def _pull_update_fix(self, cfg):
        """The pre-launch fix for the "newer version" warning: run the EXISTING
        pull, then let the screen re-check. Returns (ok, message) for the fix
        note; with the async pull the note says it is running and the screen is
        re-rendered again when the pull has finished."""
        path = (cfg.get("project_path") or "").strip()
        if not self._update_check_async:
            result = core.git_update_study(path)
            self._on_git_update_done(result, path)
            return bool(result.get("ok")), (result.get("message") or "Git Pull finished.")
        if not self.git_update_study(on_done=self._after_prelaunch_pull):
            return None
        return True, "Running Git Pull\u2026"

    def _after_prelaunch_pull(self, result):
        """The pull started from the pre-launch screen finished: show its outcome
        there and re-check (a successful pull clears the warning)."""
        dialog = getattr(self, "_briefing_dialog", None)
        try:
            if dialog is None or not dialog.top.winfo_exists() or dialog._launching:
                return
            dialog._fix_note = (bool(result.get("ok")),
                                str(result.get("reason") if not result.get("ok")
                                    else result.get("message")
                                    or "Git Pull finished."))
            dialog._rerender()
        except (tk.TclError, AttributeError):
            pass

    def github_org_clone(self):
        """GitHub button: open the clone dialog (GithubCloneDialog), which
        validates the clone INSIDE itself (busy Checking -> Cloning line, inline
        error + Retry) and only closes once the clone has succeeded; then the
        cloned folder is auto-selected as the study folder.

        The org name is a configured value (Lab Settings > GitHub), so the target
        is not hardcoded; the clone uses the login stored on this computer. All
        outcomes are decided in core.git_clone_org_repo."""
        if not self.github_org:
            self.open_lab_settings()
            return

        def on_start():
            self.github_org_btn.config(state="disabled", text="Cloning...")

        GithubCloneDialog(self.root, self.fonts, self.github_org,
                          on_start=on_start, on_finish=self._on_clone_done,
                          on_use_existing=self._use_existing_folder)

    def _use_existing_folder(self, path):
        """"Use that folder" in the clone dialog: the study is already on this
        computer, so select it (same path as Browse)."""
        path = os.path.normpath(path)
        self.var["project_path"].set(path)
        level, message = validate_project(path)
        self.log(message, {"ok": "ok", "warn": "warn", "error": "err"}[level])
        self.refresh_previews()
        self._maybe_offer_get_ready(path)

    def _on_clone_done(self, result, dialog_open=False):
        """Back on the UI thread: re-enable the button, report, and on success
        auto-select the cloned folder as the study folder (same path as Browse).
        A failure while the dialog is open is shown there (inline + Retry), so
        here it only goes to the log."""
        self.github_org_btn.config(state="normal", text=self._github_button_text())
        if result.get("ok") and result.get("path"):
            path = os.path.normpath(result["path"])
            core.remember_clone_parent(os.path.dirname(path))   # prefilled next time
            self.var["project_path"].set(path)
            level, message = validate_project(path)
            self.log(result.get("message") or ("Cloned into %s." % path), "ok")
            self.log(message, {"ok": "ok", "warn": "warn", "error": "err"}[level])
            self._maybe_offer_get_ready(path)
            return
        self.log(result.get("message") or "Could not clone the repository.", "err")
        if result.get("output"):
            self.log(result["output"], "info")
        if not dialog_open:
            messagebox.showerror(
                "Could not clone",
                "\n\n".join(x for x in (result.get("action"), result.get("message"))
                             if x) or "Could not clone the repository.",
                parent=self.root)

    def git_update_study(self, on_done=None):
        """Git Pull button (per config), also the action of the "changes on
        GitHub" banner and of the pre-launch warning: git pull in the SELECTED
        study folder (never the launcher app/ folder). core.git_update_study
        decides the outcome; the result is rendered as a section at the bottom
        of the project status (see _render_git_result). ``on_done(result)`` is
        called on the UI thread afterwards. Returns True when a pull started."""
        path = (self.var["project_path"].get() or "").strip()
        if not path:
            messagebox.showwarning(
                "No study folder",
                "Choose or clone a study folder first, then Git Pull.",
                parent=self.root)
            return False
        if self._git_pull_running:
            return False
        self._git_pull_running = True
        self.git_update_btn.config(state="disabled", text="Pulling...")
        banner_button = getattr(self, "update_pull_button", None)
        if banner_button is not None:
            try:
                banner_button.state(["disabled"])
            except tk.TclError:
                pass
        self.log("Running git pull in %s ..." % path, "info")
        self._render_git_result({"busy": True}, path)

        def worker():
            # Never pull while the background check is still fetching in the same
            # folder (two git commands at once can trip over each other's locks).
            check = self._update_thread
            if check is not None and check.is_alive():
                check.join(core.GIT_UPDATE_CHECK_TIMEOUT + 5)
            result = core.git_update_study(path)
            self.root.after(0, lambda: self._on_git_update_done(result, path, on_done))

        threading.Thread(target=worker, name="git-update", daemon=True).start()
        return True

    def _on_git_update_done(self, result, path=None, on_done=None):
        """Back on the UI thread: re-enable the button, log, and render the result
        section under the project status. A successful pull also turns the
        update banner into "Experiment up to date" (core.update_status_after_pull);
        a failed one keeps the banner, and the result section says why."""
        self._git_pull_running = False
        self.git_update_btn.config(state="normal", text=core.GIT_PULL_BUTTON_LABEL)
        level = "ok" if result.get("ok") else "err"
        self.log(result.get("message") or "Git Pull finished.", level)
        if result.get("output"):
            self.log(result["output"], "info")
        path = path or self.var["project_path"].get()
        self._render_git_result(result, path)
        after = core.update_status_after_pull(result)
        if after is not None and (path or "").strip() == self._update_status_path:
            self._update_token += 1            # a check still running is now stale
            self._update_pending = None
            self._update_status = dict(after)
            self._update_is_repo = True
            self._apply_github_sync_buttons()
        self._render_update_status()           # also re-enables the banner button
        if result.get("status") == "updated":
            self.refresh_previews()   # new files may change the detected apps
        if callable(on_done):
            on_done(result)

    def _render_git_result(self, result, path):
        """Draw the Git Pull RESULT section (the Tk twin of the web page's
        renderGitPullResult). Bold core line: "Nothing new: already up to date."
        / "Git pull failed" / "Pulled N changed files."; then a quieter line (the
        latest commit, or the plain-language reason); then COLLAPSED blocks for
        the changed-file list ("Changes pulled from Git") or git's raw output."""
        frame = self.git_result
        for child in list(frame.winfo_children()):
            child.destroy()
        self._git_result_path = (path or "").strip()
        # A successful result REPLACES the quiet "up to date" line (one line).
        self._git_result_ok = bool(result.get("ok")) and not result.get("busy")
        failed = not result.get("ok") and not result.get("busy")
        self._git_result_offers = tuple(
            name for name, key in (("login", "login_action"), ("fresh_copy", "fresh_copy"))
            if failed and result.get(key))
        self.fresh_copy_button = None
        bg = COLORS["card"]
        stamp = time.strftime("%H:%M")
        if result.get("busy"):
            head, color, sub = (result.get("busy_text") or "Pulling..."), COLORS["text"], ""
        elif result.get("ok") and result.get("status") == "current":
            head, color, sub = (result.get("message") or
                                "Nothing new: already up to date.",
                                COLORS["text"], result.get("note", ""))
        elif result.get("ok"):
            head, color, sub = (result.get("message") or "Pulled.", COLORS["ok"],
                                result.get("detail", ""))
        else:
            head, color, sub = (result.get("title") or "Git pull failed", COLORS["error"],
                                result.get("reason") or
                                ("" if result.get("title") else result.get("message", "")))
        line = tk.Frame(frame, bg=bg)
        line.pack(fill="x")
        tk.Label(line, text=head, bg=bg, fg=color, font=self.fonts.small_bold,
                 anchor="w", justify="left", wraplength=320).pack(side="left")
        tk.Label(line, text=stamp, bg=bg, fg=COLORS["faint"],
                 font=self.fonts.small).pack(side="left", padx=(6, 0))
        if sub:
            tk.Label(frame, text=sub, bg=bg, fg=COLORS["muted"],
                     font=self.fonts.small, anchor="w", justify="left",
                     wraplength=360).pack(fill="x", pady=(2, 0))
        # What to do about a failure, as a button (no terminal needed).
        if not result.get("ok") and not result.get("busy") \
                and (result.get("login_action") or result.get("fresh_copy")):
            actions = tk.Frame(frame, bg=bg)
            actions.pack(fill="x", pady=(5, 0))
            if result.get("login_action"):
                ttk.Button(actions, text=core.GITHUB_LOGIN_ACTION_LABEL,
                           style="Slim.TButton",
                           command=lambda: self.open_github_login(
                               self.git_update_study, retry=True)).pack(side="left")
            if result.get("fresh_copy"):
                self.fresh_copy_button = ttk.Button(
                    actions, text=core.GIT_FRESH_COPY_LABEL, style="Slim.TButton",
                    command=self.get_fresh_copy)
                self.fresh_copy_button.pack(side="left", padx=(
                    8 if result.get("login_action") else 0, 0))
        files = result.get("files") or []
        if result.get("ok") and files:
            fold = Collapsible(frame, "Changes pulled from Git", self.fonts.small)
            fold.pack(fill="x", pady=(4, 0))
            for entry in files:
                tk.Label(fold.inner, text=core.git_file_change_line(entry), bg=bg,
                         fg=COLORS["text"], font=self.fonts.mono, anchor="w",
                         justify="left").pack(fill="x")
        elif not result.get("ok") and result.get("output"):
            fold = Collapsible(frame, "Git output", self.fonts.small)
            fold.pack(fill="x", pady=(4, 0))
            _git_output_box(fold.inner, self.fonts, result["output"]).pack(fill="x")
        frame.pack(fill="x", pady=(8, 0), before=self._git_divider)
        self._render_update_status()

    def _clear_git_result(self):
        frame = getattr(self, "git_result", None)
        if frame is None:
            return
        for child in list(frame.winfo_children()):
            child.destroy()
        frame.pack_forget()
        self._git_result_path = ""
        self._git_result_ok = False
        self._git_result_offers = ()
        self.fresh_copy_button = None

    def _sync_git_result(self, project_path):
        """Keep the Git Pull result only while the same study folder is
        selected; otherwise hide it."""
        frame = getattr(self, "git_result", None)
        if frame is None or not self._git_result_path:
            return
        if (project_path or "").strip() != self._git_result_path:
            self._clear_git_result()

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
        preset = preset_from_fields(name, self.form_values(), author=dialog.author,
                                    lab_presets=self.lab_presets)
        # Hold the store lock across the in-memory mutation AND the save, so a
        # background last_run stamp cannot interleave and clobber this new config
        # (or vice versa). _persist re-acquires the same re-entrant lock.
        with self._store_lock:
            self.presets.append(preset)
            # Remember the author for next time (JSON-serialisable; unknown keys
            # in store_extra are preserved by save_store). The author also joins
            # the shared researcher roster (also used by the create-db dialog).
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
        # Mutate + save under the store lock so a background stamp cannot race it.
        with self._store_lock:
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
            self.banner.pack_forget()
        self.refresh_sidebar()
        self.log('Deleted the config "%s".' % name, "warn")

    def new_blank(self):
        blank = dict(DEFAULT_CONFIG)
        blank["project_path"] = ""
        # A new config's room follows the ACTIVE lab's default_room (the selector's
        # lab, else this machine's lab), not the hardcoded "study".
        try:
            ref_lab = self.var["lab"].get().strip()
        except (KeyError, tk.TclError, AttributeError):
            ref_lab = ""
        ref_lab = ref_lab or read_lab_marker() or blank.get("lab", "")
        blank["room_name"] = core.lab_default_room(self.lab_presets, ref_lab)
        # A new blank config follows this computer's default database
        # (database_id "", db_mode LAB); Save As pins it to that database's id.
        self.load_fields(blank, None)
        self.inline_status.set("muted", "")
        self.log("New blank config. Choose a project folder, then Save as new.", "info")

    # -- this computer's lab (machine.json home_lab) ----------------------

    def set_machine_lab(self, lab_id):
        """Set which lab this computer is, from the Lab Settings UI. Updates
        machine.json home_lab and narrows the main selector to that lab."""
        self._set_lab_identity(lab_id, dialog=None)

    def _set_lab_identity(self, lab_id, dialog=None, log=True):
        """Record ``lab_id`` as this machine's lab and narrow the UI to it.

        Writes machine.json home_lab (overwriting any previous choice, this is
        the UI-settable path), makes that lab the only displayed one via the existing
        display toggles, points the built-in default at it, persists, and
        repaints. Shared by the first-run chooser and the Lab Settings control.
        """
        lab_id = str(lab_id)
        try:
            set_lab_marker(lab_id)
        except (OSError, ValueError) as error:
            messagebox.showerror(
                "Could not set the lab",
                "This computer's lab could not be saved:\n\n%s" % error, parent=self.root)
            return
        # Make it the ONLY displayed lab (reuse the display-toggle machinery), so
        # the machine shows just its one lab. The store mutations + save happen
        # under the store lock so a background worker's save cannot race them
        # (_persist_store re-acquires the same re-entrant lock).
        ok, _msg, new_list = core.apply_lab_identity(self.lab_presets, lab_id)
        with self._store_lock:
            if ok:
                self.store_extra["lab_presets"] = new_list
                self.lab_presets = core.lab_presets_from_store(self.store_extra)
            # Point the built-in default's lab (and room) at this machine's lab.
            apply_lab_marker(self.presets, lab_id, lab_presets=self.lab_presets)
            self._persist_store()
        if dialog is not None:
            try:
                dialog.destroy()
            except tk.TclError:
                pass
        # Repaint the main selector to the single lab and reflect it in the form.
        # Pass the open config's lab so, if it is a hidden-but-open config, its
        # own lab keeps a tile (BUG B) rather than vanishing on the identity change.
        self._rebuild_lab_tiles(config_lab=self.var["lab"].get() if getattr(self, "var", None) else None)
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
            self.log("This computer is set to %s." % label, "ok")

    def _mutate_store(self, mutate):
        """Serialise one read-modify-write-save of the store under the app lock.

        Holds ``self._store_lock`` across BOTH the mutation callback and the
        ``save_store``, so no two threads can interleave their read-modify-write-
        save sequences (a lost update) and no thread can mutate ``self.presets`` /
        ``self.store_extra`` while another is serialising them to disk. Used by
        the background workers (launch stamp, create-database registration) and
        by the store-writing dialogs. Returns whatever ``mutate`` returns; a
        ``save_store`` OSError propagates (the in-memory change is still applied).
        The lock is re-entrant, so a callback may itself call another lock-guarded
        save without deadlocking.
        """
        with self._store_lock:
            # Cross-process lost-update guard: if another process wrote the store
            # since our last save, merge its change in (identity-preserving) before
            # applying ours, so e.g. a headless --run last_run stamp is not lost.
            self._reconcile_store()
            result = mutate()
            save_store(self.presets, self.store_extra, self.store_path)
            self._store_mtime = self._store_mtime_now()
            return result

    def _store_mtime_now(self):
        """The saved_configs.json modification time, or None when it does not exist."""
        try:
            return os.path.getmtime(self.store_path)
        except OSError:
            return None

    def _reconcile_store(self):
        """Re-read the store and merge in another process's changes when the file
        changed on disk since our last save (see core.merge_store_from_disk).
        Called under the store lock at the top of _mutate_store."""
        current = self._store_mtime_now()
        if current is None or current == self._store_mtime:
            return
        try:
            disk_presets, disk_extra = load_store(self.store_path)
        except Exception:
            return
        self.presets, self.store_extra = core.merge_store_from_disk(
            self.presets, self.store_extra, disk_presets, disk_extra)
        # Keep the derived state consistent after adopting disk content: the live
        # LAB_DB, the lab presets, and the app-owned built-in Lab default.
        core.apply_default_database(self.store_extra)
        self.lab_presets = core.lab_presets_from_store(self.store_extra)
        apply_lab_marker(self.presets, lab_presets=self.lab_presets)
        clear_builtin_last_run(self.presets)
        clear_builtin_project_path(self.presets)

    def _persist(self):
        # Lock-guarded so a background worker's stamp-and-save cannot serialise
        # the store while this save is mid-write (or vice versa).
        with self._store_lock:
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
        BlockDialog(self.root, self.fonts, state, self.add_block, self.log,
                    on_show_log=self.reveal_activity_log)
        self.refresh_block_status()

    def add_block(self):
        """Append the block to settings.py, after a timestamped backup. Runs on
        an explicit click of the "Get ready for the lab" button (warning strip or
        block viewer); refuses when the block is already there."""
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
        # No Yes/No box (N12): pressing the clearly-labelled button IS the
        # explicit click. The safeguards stay: a timestamped .bak first, and the
        # refusal to append twice (above, and inside append_block).
        try:
            backup, path = append_block(cfg["project_path"])
        except core.BlockAlreadyPresent:
            # A race: another appender got the block in between our check above
            # and the lock. The safe writer refused, so report it as already-there.
            messagebox.showinfo("Already there",
                                "settings.py already has the oTree lab support block.",
                                parent=self.root)
            return False
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
        missing, cut off, overridden by a later ROOMS line, or STALE (an outdated
        block body); shown at most once per selection because this is the only
        place that calls it. A stale/outdated block offers a REFRESH (which
        replaces it in place) instead of an append."""
        state = inspect_settings(path, self._lab_room())
        if not state["readable"]:
            return
        # A present-but-outdated block (stale, or cut-off) -> REFRESH replaces it.
        if state.get("needs_refresh"):
            GetReadyDialog(
                self.root, self.fonts, self._refresh_block_for_lab,
                headline="This project's oTree lab support block is out of date.",
                subline="One click refreshes it to the current block.",
                button_label="Refresh lab block")
            return
        not_ready = (not state["has_block"] or not state["complete"]
                     or state["rooms_after"])
        if not not_ready:
            return
        GetReadyDialog(self.root, self.fonts, self._get_ready_for_lab)

    def _lab_room(self):
        """This machine/config lab's default room, for the settings inspector."""
        try:
            lab = self.var["lab"].get().strip()
        except (KeyError, tk.TclError, AttributeError):
            lab = ""
        lab = lab or read_lab_marker() or ""
        return core.lab_default_room(self.lab_presets, lab)

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
        except core.BlockAlreadyPresent:
            return False, ("settings.py already has the lab support block, so nothing was added. "
                           "If a ROOMS line follows the block, move the block to the end.")
        except OSError as error:
            self.log("Could not add the block: %s" % error, "err")
            return False, "Could not update settings.py: %s" % error
        self.log("Backed up settings.py to %s" % backup, "ok")
        self.log("Appended the oTree lab support block to %s" % path, "ok")
        self.refresh_block_status()
        return True, "Done. Now press Launch, pick your lab, and go."

    def _refresh_block_for_lab(self):
        """Replace an OUTDATED lab support block with the current one.

        Uses core.refresh_block (same lock/atomic/backup machinery as append):
        it removes the old block region and appends the current block, one atomic
        op with a timestamped .bak. Fixes a project stuck on an old block (the
        Micro 1 case). Returns (ok, message) for the popup to show inline."""
        cfg = self.form_values()
        state = inspect_settings(cfg["project_path"], self._lab_room())
        if not state["readable"]:
            return False, "Could not read settings.py, so nothing was changed."
        if not state["has_block"]:
            # No block to refresh -> fall back to a normal append.
            return self._get_ready_for_lab()
        try:
            backup, path = refresh_block(cfg["project_path"])
        except OSError as error:
            self.log("Could not refresh the block: %s" % error, "err")
            return False, "Could not update settings.py: %s" % error
        self.log("Backed up settings.py to %s" % backup, "ok")
        self.log("Refreshed the oTree lab support block in %s" % path, "ok")
        self.refresh_block_status()
        return True, "Refreshed. Now press Launch, pick your lab, and go."

    # -- one-click shortcut ------------------------------------------------

    def save_shortcut(self):
        """Save a LIVE one-click shortcut for the current SAVED config.

        The shortcut calls the launcher headlessly (``otree_lab_launcher.py
        --run "<name>"``) so it always reflects the latest saved settings and
        the DB password is NOT baked into a loose file -- the secret stays in
        saved_configs.json. It therefore requires a saved, unmodified config; if the
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
        # Default suggested filename: the "Launch_"-prefixed name from core
        # (e.g. MyStudy -> Launch_MyStudy.vbs), matching the web app. Only the
        # SUGGESTION changes -- the user can still rename it, and the shortcut's
        # contents/behaviour are untouched (they come from core.headless_shortcut).
        suggested = shortcut["filename"]
        target = filedialog.asksaveasfilename(
            parent=self.root, title="Save one-click shortcut",
            defaultextension=shortcut["ext"], initialfile=suggested,
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
        # A macOS/Unix shell shortcut (.command/.sh, or a shebang script) must be
        # executable or the OS refuses to run it. Windows .vbs/.bat need no exec bit.
        target_lower = target.lower()
        if (target_lower.endswith(".command") or target_lower.endswith(".sh")
                or shortcut["content"].startswith("#!")):
            try:
                os.chmod(target, os.stat(target).st_mode | 0o111)
            except OSError as error:
                self.log("Could not mark the shortcut executable: %s" % error, "warn")
        self.log("Saved one-click shortcut: %s" % target, "ok")
        self.log('Double-click it to launch the saved config "%s". The database '
                 "password is not stored in the file." % name, "muted")

    # -- Lab Settings (admin config + lab presets, Feature 4) --------------

    def open_lab_settings(self):
        LabSettingsDialog(self.root, self.fonts, self)

    def _persist_store(self):
        """Save presets + extra (which now carries lab_presets and pg_admin).

        Lock-guarded (see :meth:`_persist`) so it cannot race a background
        worker's stamp-and-save.
        """
        with self._store_lock:
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
        # A lab's default_room may have just changed; re-derive the built-in Lab
        # default's room from the machine lab live (marker unset -> no-op).
        apply_lab_marker(self.presets, lab_presets=self.lab_presets)
        # JOB 1: if the built-in Lab default is the config on screen its room
        # FOLLOWS the lab, so re-apply the (possibly changed) lab room to the form
        # NOW -- otherwise the "Room X (lab default)" label stays stale until a
        # restart. Only when unedited (not dirty), and under the loading guard so
        # it does not count as a user edit.
        if (self.selected_index is not None
                and 0 <= self.selected_index < len(self.presets)):
            selected = self.presets[self.selected_index]
            if is_builtin(selected) and not self.dirty:
                new_room = normalize_config(selected)["room_name"]
                if self.var["room_name"].get().strip() != new_room:
                    self.loading = True
                    try:
                        self.var["room_name"].set(new_room)
                    finally:
                        self.loading = False
                self._update_room_default_tag()
        # Keep the open config's own lab in the selector even if it was just
        # hidden in Lab Settings (BUG B): pass the form's current lab.
        self._rebuild_lab_tiles(config_lab=self.var["lab"].get() if getattr(self, "var", None) else None)
        self._apply_lab_view()
        self.refresh_previews()

    # -- Create a new database (Feature 2) ---------------------------------

    def create_database_dialog(self, from_settings=False, parent=None, on_done=None):
        # No admin-details gate here (web parity): registering a database that
        # already exists on another host needs no admin login, and a create
        # without admin details is refused by core.create_database with the
        # "Fill in the Postgres admin details" message.
        # ``from_settings`` (Lab Settings > Add a database): the database joins
        # this computer's list only; the config on screen is left alone (N3).
        roster = core.list_researchers(self.store_extra, self.presets)
        suggested_researcher = str(self.store_extra.get("last_author", "")).strip() or default_author()
        return CreateDatabaseDialog(parent or self.root, self.fonts, self,
                                    suggested_name=self._suggested_db_name(),
                                    researchers=roster,
                                    suggested_researcher=suggested_researcher,
                                    from_settings=from_settings, on_done=on_done)

    def _suggested_db_name(self):
        """A database name prefilled from the current project FOLDER (or, failing
        that, the open config's name), slugified to a valid Postgres identifier by
        the shared ``core.slugify_pg_dbname`` so both launchers prefill the same
        way. Returns "" only when even the slug fails validation."""
        source = final_folder_name(self.var["project_path"].get())
        if not source and self.selected_index is not None \
                and 0 <= self.selected_index < len(self.presets):
            source = str(self.presets[self.selected_index].get("name", "")).strip()
        if not source:
            return ""
        slug = core.slugify_pg_dbname(source)
        ok, _msg = core.validate_pg_identifier(slug)
        return slug if ok else ""

    def _run_create_database(self, name, user, password, researcher, on_done,
                             host="", port=""):
        """Do the create off the UI thread and hand the result back on it. On a
        confirmed create, ALSO register the database in the global registry with
        its creator researcher (the Postgres user is recorded separately), so it
        appears in every config's picker afterward."""
        admin = core.pg_admin_from_store(self.store_extra)
        refusal = core.database_host_refusal(host)
        if refusal:
            self._on_main(lambda: on_done({"ok": False, "reason": "localhost_only",
                                           "message": refusal}))
            return
        # host/port = the dialog's Host/Port; core refuses a non-local host.
        result = core.create_database(admin, name, new_user=user, new_password=password,
                                      host=host or admin["admin_host"],
                                      port=port or admin["admin_port"])
        if result.get("ok"):
            fields = result.get("fields") or {}
            try:
                # Worker thread: register + save atomically under the store lock so
                # it cannot race a UI save (or the launch stamp).
                entry = self._mutate_store(lambda: core.register_database(
                    self.store_extra, title=name, researcher=researcher,
                    connection=fields, postgres_user=fields.get("db_user", "")))
                result["registered"] = entry
            except Exception as error:   # registration must never lose the DB
                result["register_error"] = str(error)
        self._on_main(lambda: on_done(result))

    def _run_register_existing_database(self, name, user, password, researcher, on_done,
                                        host="", port=""):
        """Register an ALREADY-EXISTING database (the create dialog's "already
        exists" checkbox): record the connection in the global registry WITHOUT
        running CREATE DATABASE. Host/port are the dialog's Host/Port fields
        (blank = the Lab Settings admin host/port) and a blank user/password falls
        back to the admin role, so the resulting registry entry is IDENTICAL in
        shape to a freshly-created one and is selectable/editable afterward."""
        admin = core.pg_admin_from_store(self.store_extra)
        refusal = core.database_host_refusal(host)
        if refusal:
            self._on_main(lambda: on_done({"ok": False, "reason": "localhost_only",
                                           "message": refusal}))
            return
        fields = core.existing_database_fields(admin, name, user, password,
                                               host=host, port=port)
        result = {"ok": True, "created_db": False, "registered_only": True,
                  "fields": fields,
                  "message": "Registered the existing database %r (not created)." % name}
        try:
            entry = self._mutate_store(lambda: core.register_database(
                self.store_extra, title=name, researcher=researcher,
                connection=fields, postgres_user=fields.get("db_user", "")))
            result["registered"] = entry
        except Exception as error:
            result["ok"] = False
            result["message"] = "Could not register the database: %s" % error
        else:
            # Feature 1: non-blocking test-connect for a REMOTE registered
            # database (parity with the web _advise_remote_connection). Local host:
            # nothing to check. Remote: try a short connect, but NEVER undo the
            # registration -- on a failure just append an advisory to the message.
            # Safe to run here: this method is already on a worker thread.
            if core.is_local_host(fields.get("db_host", "")):
                # Local: the same login check a create gets (web parity). Kept
                # either way; a refused login is a clear warning.
                warning = core.registered_database_login_warning(fields)
                if warning:
                    result["warning"] = warning
                    result["message"] = (result.get("message", "").rstrip()
                                         + " " + warning)
            self._advise_remote_connection(fields, result)
        self._on_main(lambda: on_done(result))

    def _advise_remote_connection(self, fields, result):
        """Non-blocking connect check for a REMOTE registered database. On a local
        host it does nothing. On a remote host it tries a short connect and either
        notes it reachable or appends an ADVISORY -- registration is never undone.
        Mirror of otree_launcher_web.py's Api._advise_remote_connection."""
        host = fields.get("db_host", "")
        if core.is_local_host(host):
            return
        check = core.test_database_connection(
            fields.get("db_name", ""), fields.get("db_user", ""),
            fields.get("db_password", ""), host, fields.get("db_port", ""))
        result["connect_test"] = check
        if check.get("ok"):
            result["reachable"] = True
            result["message"] = (result.get("message", "").rstrip()
                                  + " Connected to %s successfully; the database is reachable."
                                  % host)
        else:
            result["reachable"] = False
            result["message"] = (result.get("message", "").rstrip()
                                  + " Could not connect to %s. The database has been "
                                    "registered anyway. Before using it, verify the database "
                                    "exists on that machine set up this way, and that the host "
                                    "is switched on and reachable." % host)

    def apply_created_database(self, fields):
        """Auto-fill the Custom database config from a confirmed create, and save
        it as the current on-screen config (a Custom setup)."""
        # Point the config at the registered entry BY ID (the saved reference).
        entry = None
        if fields.get("database_id"):
            entry = core.find_database(self.store_extra, fields["database_id"])
        if entry is None:
            entry = core.find_database_by_connection(
                fields, core.known_databases_from_store(self.store_extra))
        if entry is not None:
            fields = core.database_config_fields(entry)
        self.db_mode_label.set(DB_MODE_LABELS[DB_MODE_CUSTOM])
        self.var["db_mode"].set(DB_MODE_CUSTOM)
        for key in ("database_id", "db_name", "db_user", "db_password", "db_host", "db_port"):
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
        # Block-aware: when the project has a live lab support block the picker
        # also offers the lab room the block defines at launch (this lab's own
        # default_room), so a just-appended block does not hide the room needed.
        return core.enumerate_rooms_for_picker(project_path, self._lab_room())

    def set_room(self, name):
        name = (name or "").strip() or self._lab_room()
        self.var["room_name"].set(name)
        self.log("Room set to %r for this run." % name, "info")

    def _update_room_default_tag(self):
        """Show the muted "(lab default)" tag whenever the room IS this lab's
        default room (which may have been changed in Lab Settings), not a
        hardcoded "study" -- parity with the web roomTag (Job 1)."""
        tag = getattr(self, "room_default_tag", None)
        if tag is None:
            return
        is_default = (self.var["room_name"].get().strip() == self._lab_room())
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
        # An update-check answer from hours ago is asked again now (in the
        # background; the launch screen redraws itself if a newer version turns up).
        self._recheck_update_if_stale(cfg.get("project_path", ""))
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
        needs_block = self._project_needs_block(cfg)
        if needs_block:
            # ONE issue with ONE button for a project that is not lab-ready (N6):
            # the same wording and the same action name as everywhere else.
            issues.append({
                "level": "warn",
                "title": core.NO_BLOCK_ISSUE_TITLE,
                "hint": core.NO_BLOCK_ISSUE_HINT,
                "fix": self._get_ready_for_lab, "fix_label": core.GET_READY_LABEL,
                "info": "block"})
        # "Use a file from my project" with no file: said out loud (N2). A warning,
        # never a block: the launch falls back to an open room.
        seat_warning = core.seat_file_missing_warning(cfg)
        if seat_warning:
            issues.append({"level": "warn", "title": seat_warning, "hint": "",
                           "fix": None, "fix_label": ""})
        for failure in core.preflight_failures(core.preflight(cfg, core.load_lab_info())):
            # The room is not in the project's static ROOMS, but the lab support
            # block is present and seats will be written: the block defines that
            # room at launch, so the blocker is resolved. This is what clears the
            # room issue after the block is appended.
            if failure.get("kind") == "room" and self._room_will_be_defined_by_block(cfg):
                continue
            # While the block is MISSING (and seats are used) the room issue is the
            # same problem with the same fix as the issue above: not shown twice.
            if failure.get("kind") == "room" and needs_block:
                continue
            # A check core marks ``blocking`` (the launch port already answers: a
            # session is running) is a MUST-FIX, not a warning (N1).
            issue = {"level": "block" if failure.get("blocking") else "warn",
                     "title": failure.get("message", ""),
                     "hint": "" if failure.get("blocking") else str(failure.get("detail", "")),
                     "fix": None, "fix_label": ""}
            if failure.get("field"):
                issue["field"] = failure["field"]
            self._attach_inline_fix(issue, failure, cfg)
            issues.append(issue)
        # Non-blocking: the lab's default room changed since this config was saved.
        # One click switches THIS config to the lab default; ignoring it and
        # launching as-is is fine (silent when the rooms already match).
        # The background update check found newer commits on GitHub for THIS
        # folder: an amber warning (never a block) whose fix is the existing Git
        # Pull. A check that is still running is not waited for; Re-check picks
        # its result up.
        if cfg["project_path"].strip() == self._update_status_path:
            update_warning = core.prelaunch_update_warning(self._update_status)
            if update_warning:
                issues.append({
                    "level": "warn", "title": update_warning, "hint": "",
                    "fix": lambda cfg=cfg: self._pull_update_fix(cfg),
                    "fix_label": core.GIT_UPDATE_ACTION_LABEL, "info": ""})
        # Only for a SAVED config that still has the room it was saved with (N8):
        # a room just picked on screen is the user's choice, already stated in
        # the launch summary.
        saved = None
        if (self.selected_index is not None
                and 0 <= self.selected_index < len(self.presets)
                and not is_builtin(self.presets[self.selected_index])):
            saved = self.presets[self.selected_index]
        mismatch = core.launch_room_mismatch(cfg, saved, self.lab_presets)
        if mismatch:
            def _use_lab_room(cfg=cfg, room=mismatch["lab_room"]):
                cfg["room_name"] = room
                self.set_room(room)
                return True, "Room set to %r (the lab default)." % room
            issues.append({
                "level": "warn", "title": mismatch["message"],
                "hint": "Existing saved configs keep their room; this switches only "
                        "this config. You can also just launch as-is.",
                "fix": _use_lab_room, "fix_label": "Use new default", "info": ""})
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
                cfg["database_id"] = core.DB_BUILTIN_SQLITE
                self.var["db_mode"].set(core.DB_MODE_NONE)
                self.var["database_id"].set(core.DB_BUILTIN_SQLITE)
                self.apply_db_mode()
                return True, ("Switched to the oTree default database (SQLite). "
                              "Postgres and psycopg2 are no longer needed.")
            issue["fix"] = _to_sqlite
            issue["fix_label"] = meta.get("fix_label", "Change to SQLite")
        elif tag == "recheck":
            issue["fix"] = lambda: (True, "Re-checked.")
            issue["fix_label"] = meta.get("fix_label", "Re-check")
        elif tag == "use_lab_default":
            def _to_lab_default(cfg=cfg):
                # The chosen database could not be connected to: switch to this
                # computer's default database so a re-launch works. Mutate the
                # launch cfg AND the main form so the change sticks either way.
                switched = core.switch_to_lab_default(cfg)
                for key in ("database_id", "db_mode", "db_name", "db_user",
                            "db_password", "db_host", "db_port"):
                    cfg[key] = switched[key]
                    if key in self.var:
                        self.var[key].set(switched[key])
                self.db_mode_label.set(DB_MODE_LABELS[switched["db_mode"]])
                self.apply_db_mode()
                self.refresh_previews()
                return True, ("Switched to this computer's default database. "
                              "Re-checking the connection...")
            issue["fix"] = _to_lab_default
            issue["fix_label"] = meta.get("fix_label", "Use this computer's default database")
        elif tag == "pick_room":
            issue["kind"] = "room_pick"
            issue["rooms"] = meta.get("rooms", [])
            if issue["rooms"]:
                # The inline picker below lists the rooms: do not print them twice.
                issue["hint"] = ""

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
            on_open_dashboard=lambda: self._reopen_dashboard(cfg),
            on_show_log=self.reveal_activity_log)

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
                "address, or pick a lab.")

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
            # Bind the message now: Python deletes ``error`` when the except
            # block ends, and _on_main runs the callback later.
            self._on_main(lambda msg="Launch stopped: %s" % error: self.inline_status.set(
                "error", msg))
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
        if not ok:
            # The popup may be closed: the failure also stays on the status line,
            # always with the [See activity log] button (the details are there).
            reason = core.split_activity_log_hint(str(info or "Launch failed."))[0]
            try:
                self.inline_status.set("error", (reason or "Launch failed.")
                                       + " See the activity log.")
            except tk.TclError:
                pass
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

        # The last guard, BEFORE anything is written or reset (N1): a server
        # already answering on the launch port means a session is running. A
        # relaunch would reset ITS database and then report the old server's
        # answer as success, so refuse here whatever the screen allowed.
        running = core.running_server_problem(cfg)
        if running:
            self.log(running, "err")
            self._on_main(lambda msg=running: self.inline_status.set("error", msg))
            return False, running

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
            # Bind the message now: Python deletes ``error`` when the except
            # block ends, and _on_main runs the callback later.
            self._on_main(lambda msg="Could not write the seat file: %s" % error: self.inline_status.set(
                "error", msg))
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
        # ONE line for the whole environment (passwords masked), not one per
        # variable. What is NOT set (no DATABASE_URL on SQLite, no label file) is
        # already said by the lines around it.
        self.log(core.env_log_line(keys, env), "muted")

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

        server_start = time.monotonic()
        if not self._start_server(cfg, path, env):
            self._record_session(cfg, config_name, "fail", None)
            return False, "The oTree server could not be started. See the activity log above."

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
        monitor_url = "http://%s:%s%s" % (
            core.AUTOLOGIN_HOST, cfg["port"], core.room_monitor_path(cfg["room_name"]))
        if cfg["open_browser"]:
            self.log("Waiting for the server to respond, then opening the dashboard.", "info")
            result = core.open_dashboard_authenticated(
                core.AUTOLOGIN_HOST, cfg["port"], cfg["room_name"],
                cfg["admin_username"], cfg["admin_password"], auto_login=use_auto,
                on_wait=lambda s: self.log(
                    "Still waiting for the server to respond (%d s)…" % s, "muted"))
            if result["method"] == "cookie":
                self.log("Opened the dashboard already logged in (auto-login: form-login + "
                         "cookie relay): %s" % result["monitor_url"], "ok")
            else:
                self.log("Opened the dashboard login page: %s" % result["monitor_url"], "info")
                self.log("    %s. Log in with the admin username and password shown in the popup."
                         % result["reason"], "muted", prefix=False)
        else:
            # No browser open: still confirm the server really came up before
            # reporting success, so a crash is not mis-reported as "Launched".
            self.log("Open in browser is off. Confirming the server is up. The page would be %s"
                     % monitor_url, "muted")
            ready = core.wait_for_server(
                core.AUTOLOGIN_HOST, cfg["port"],
                on_wait=lambda s: self.log(
                    "Still waiting for the server to respond (%d s)…" % s, "muted"))
            result = {"ok": True, "method": "manual", "monitor_url": monitor_url,
                      "server_ready": ready}

        if not result.get("server_ready"):
            # A startup crash / missing project / port race: prodserver never
            # answered. Do NOT stamp last_run or claim success -- the real
            # traceback is in the server terminal window. Fail-soft: a page may
            # already be open (manual fallback), so the operator can still retry.
            msg = self._startup_failure_message()
            self.log(msg, "err")
            self._on_main(lambda: self.inline_status.set("error", msg))
            self._record_session(cfg, config_name, "fail", None)
            return False, msg

        # Only a real, server-ready success stamps the run and claims "Launched".
        ready_seconds = time.monotonic() - server_start
        self._stamp_last_run(cfg)
        self._record_session(cfg, config_name, "ok", ready_seconds)
        self._on_main(lambda: self.inline_status.set(
            "ok", "Server started. Its window stays open; press Ctrl-C there to stop it."))
        self.log("Done. The server keeps running in its own window.", "ok")
        # The launch briefing popup is still open in its "launching" state; on
        # this real success it switches to the post-launch takeover banner, which
        # reports HONESTLY whether the dashboard opened logged in (cookie) or at
        # the login page (manual), via _deliver_launch_result.
        return True, result

    def _startup_failure_message(self):
        """The message shown when prodserver never became ready.

        Points the operator to the server terminal window (the real traceback is
        there) and, when the linux-background child has already exited, surfaces
        its exit code."""
        base = ("The oTree server did not become ready — it may have crashed on "
                "startup (a missing project, a database error, or the port already "
                "in use). Nothing was marked as launched. Check the server terminal "
                "window for the real error.")
        proc = getattr(self, "_server_proc", None)
        try:
            if proc is not None:
                code = proc.poll()
                if code is not None:
                    base += " (The server process already exited with code %s.)" % code
        except Exception:
            pass
        return base

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
            # Bind the message now: Python deletes ``error`` when the except
            # block ends, and _on_main runs the callback later.
            self._on_main(lambda msg="Could not run otree resetdb: %s" % error: self.inline_status.set(
                "error", msg))
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
        # Remember the child on the linux-background path so a launch that fails
        # its readiness poll can surface an early child exit (elsewhere the server
        # runs detached in its own terminal, so there is no handle to poll).
        self._server_proc = None
        try:
            if spec["kind"] == "linux-background":
                process = subprocess.Popen(
                    spec["cmd"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    universal_newlines=True, bufsize=1, **kwargs)
                self._server_proc = process
                threading.Thread(target=self._pipe_to_log, args=(process,), daemon=True).start()
            else:
                subprocess.Popen(spec["cmd"], **kwargs)
        except OSError as error:
            self.log("Could not start otree prodserver: %s" % error, "err")
            # Bind the message now: Python deletes ``error`` when the except
            # block ends, and _on_main runs the callback later.
            self._on_main(lambda msg="Could not start otree prodserver: %s" % error: self.inline_status.set(
                "error", msg))
            return False
        self.log("otree prodserver started.", "ok")
        return True

    def _pipe_to_log(self, process):
        for line in process.stdout:
            line = line.rstrip()
            if line:
                self.log(line, "out", prefix=False)

    def _record_session(self, cfg, config_name, outcome, ready_seconds):
        """Append ONE launch line to data/sessions.jsonl (fable review F).

        Fail-soft in core, so it can never break a launch; logged alongside the
        last_run stamp so every launch (success or a ready-failure) is recorded.
        The author is the selected config's saved author when there is one.
        """
        author = ""
        if self.selected_index is not None:
            author = self.presets[self.selected_index].get("author", "")
        core.record_session(
            cfg, config_name=config_name, author=author, outcome=outcome,
            server_ready_seconds=ready_seconds, lab_presets=self.lab_presets)

    def _stamp_last_run(self, cfg):
        # Stamp the SELECTED config by identity (its index), even if its room (or
        # any field) was edited on-screen and never saved -- otherwise a launch
        # after a "(lab default)" room change would leave the config showing
        # "never run" (parity with the web _mark_run identity stamp). Only a
        # brand-new unsaved scratch (no selection) has nothing to stamp.
        if self.selected_index is None:
            self.log("These settings are not a saved config, so no config was stamped. "
                     "Use Save as new to keep them.", "muted")
            return
        preset = self.presets[self.selected_index]
        # The built-in Lab default is a launch TEMPLATE you launch FROM, never a
        # saved config, so it must NEVER record a run (Job 2). Keep the same muted
        # "not a saved config" guidance in the log and do not stamp it.
        if is_builtin(preset):
            self.log("The Lab default is a launch template, so nothing was stamped. "
                     "Use Save as new to keep these settings.", "muted")
            return
        # Runs on the launch WORKER thread, so stamp + save go through the store
        # lock: a UI-thread "save as new" cannot interleave and lose either change.
        try:
            self._mutate_store(lambda: preset.__setitem__("last_run", now_iso()))
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
    a headline, one line, one primary button, a 'What this changes' link and a
    dismiss. 'What this changes' expands the explanation in place (no navigating
    away). The primary button appends the lab support block through the app callback,
    which reuses the same backup and refuse-to-append-twice guard as the manual
    button. Like the other modals it dims the launcher and grab_sets, but it is
    freely dismissible so a non-lab project chosen on purpose closes cleanly.
    """

    WRAP = 372

    def __init__(self, parent, fonts, on_ready, headline=None, subline=None,
                 button_label=None):
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
            text=headline or "This project is not lab-ready yet.",
            bg=COLORS["card"], fg=COLORS["text"], font=fonts.bold,
            anchor="w", justify="left", wraplength=self.WRAP)
        self.headline.grid(row=0, column=0, sticky="ew")

        self.subline = tk.Label(
            body,
            text=subline or ("One click gives you:\n"
                             "  •  Participant PC links\n"
                             "  •  Seat labels\n"
                             "  •  Lab database"),
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

        self.more = tk.Label(body, text="What this changes  ▾", bg=COLORS["card"],
                             fg=COLORS["muted"], font=fonts.small, cursor="hand2",
                             anchor="w")
        self.more.grid(row=3, column=0, sticky="w", pady=(12, 0))
        self.more.bind("<Button-1>", lambda _e: self._toggle_info())

        buttons = tk.Frame(body, bg=COLORS["card"])
        buttons.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        self.dismiss = ttk.Button(buttons, text="Not now", command=self._close)
        self.dismiss.pack(side="right")
        self.primary = tk.Button(
            buttons, text=button_label or core.GET_READY_LABEL,
            command=self._get_ready,
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
            "A timestamped .bak copy of your settings.py is saved first, right next to "
            "it — that .bak is the ONLY file the launcher writes into your project "
            "(its lock file lives in the launcher's own data folder, not here). Every "
            "override is guarded by the launcher's environment variables, so with no "
            "lab environment set your project is byte-for-byte unchanged. It is fully "
            "revertible: delete from the banner line to the end of settings.py.")

    def _toggle_info(self):
        self.expanded = not self.expanded
        if self.expanded:
            self.info.grid(row=2, column=0, sticky="ew", pady=(11, 0))
            self.more.configure(text="What this changes  ▴")
        else:
            self.info.grid_remove()
            self.more.configure(text="What this changes  ▾")
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

    def __init__(self, parent, fonts, state, add_callback, log, on_show_log=None):
        self.parent = parent
        self.add_callback = add_callback
        self.log = log
        self.on_show_log = on_show_log
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
        if on_show_log is not None:
            # [See activity log] closes this viewer (it holds the grab) first.
            self.status.on_show_log = lambda: (top.destroy(), on_show_log())
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
        self.add_button = ttk.Button(buttons, text=core.GET_READY_LABEL, command=self._add)
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
                                  "beside it. See the activity log for both paths.")


class Collapsible(tk.Frame):
    """A de-emphasised "▸ Title" toggle over a body frame that starts COLLAPSED
    (the Tk stand-in for an HTML <details>). Put the hidden content in
    ``self.inner``."""

    def __init__(self, master, title, font, bg=None):
        bg = bg or COLORS["card"]
        tk.Frame.__init__(self, master, bg=bg)
        self._title = title
        self._open = False
        self.toggle = tk.Label(self, text="▸  " + title, bg=bg, fg=COLORS["muted"],
                               font=font, anchor="w", cursor="hand2")
        self.toggle.pack(fill="x")
        self.toggle.bind("<Button-1>", lambda _e: self.set_open(not self._open))
        self.inner = tk.Frame(self, bg=bg)

    def set_open(self, value):
        self._open = bool(value)
        self.toggle.configure(text=("▾  " if self._open else "▸  ") + self._title)
        if self._open:
            self.inner.pack(fill="x", pady=(2, 0))
        else:
            self.inner.pack_forget()

    @property
    def is_open(self):
        return self._open


def _git_output_box(master, fonts, text):
    """A read-only monospace box holding git's raw output (for a Collapsible)."""
    lines = text.splitlines() or [""]
    box = tk.Text(master, height=min(max(len(lines), 2), 10), wrap="word",
                  font=fonts.mono, bg=COLORS["field_off"], fg=COLORS["text"],
                  relief="flat", highlightthickness=1,
                  highlightbackground=COLORS["card_line"], padx=6, pady=4)
    box.insert("1.0", text)
    box.configure(state="disabled")
    return box


def _call_login_done(callback, ok, message, saved):
    """Call a login dialog's ``on_done`` with (ok, message, saved); an older
    two-argument callback (ok, message) still works."""
    try:
        import inspect
        params = inspect.signature(callback).parameters
        takes_three = len(params) >= 3 or any(
            p.kind == p.VAR_POSITIONAL for p in params.values())
    except (TypeError, ValueError):
        takes_three = True
    if takes_three:
        callback(ok, message, saved)
    else:
        callback(ok, message)


class GithubCloneDialog(object):
    """Get (clone) a study from the lab's GitHub organisation, validated INSIDE
    the dialog.

    ONE thing to fill in: the repository (pick it from the list of the
    organisation's repositories this computer's login can see, type its name, or
    paste a full GitHub link). The folder it is saved into is PREFILLED (the one
    used last time on this computer, else local/); "Change..." picks another.
    Return clones. Pressing Clone does NOT close the dialog: it shows a busy
    line (Checking -> Cloning) and runs core.git_clone_org_repo on a worker
    thread. On failure the dialog stays open with the name intact and an inline
    error: the BOLD line says what to do (core's ``action``), what happened
    follows, git's raw output is collapsed, and Clone reads Retry. A folder that
    already exists offers itself ("Use that folder"). Only success closes it.
    ``on_finish(result, dialog_open)`` is called on the UI thread whenever a clone
    finishes (success, or failure after the user closed the dialog), so the owner
    can auto-select the folder and restore its toolbar button.
    """

    def __init__(self, parent, fonts, org, on_start, on_finish, on_use_existing=None,
                 repo_lister=None):
        self.parent = parent
        self.fonts = fonts
        self.org = org
        self.on_start = on_start
        self.on_finish = on_finish
        self.on_use_existing = on_use_existing
        self.dest = core.clone_parent_default()
        self.running = False
        self.alive = True
        top = self.top = tk.Toplevel(parent)
        top.title("Get a study from %s" % org)
        top.configure(bg=COLORS["card"])
        _attach_shade(parent, top)
        top.transient(parent)
        top.resizable(False, False)
        top.protocol("WM_DELETE_WINDOW", self.cancel)
        bg = COLORS["card"]

        body = self.body = tk.Frame(top, bg=bg)
        body.pack(fill="both", expand=True, padx=18, pady=16)
        head = tk.Frame(body, bg=bg)
        head.pack(fill="x")
        tk.Label(head, text="Get a study from %s" % org, bg=bg,
                 fg=COLORS["text"], font=fonts.bold, anchor="w").pack(side="left")
        # The explanation is behind the info tip (core.UI_TIPS["clone"]).
        self.tip = info_tip(head, core.ui_tip("clone"), font=fonts.small)
        self.tip.pack(side="left")

        tk.Label(body, text="Repository", bg=bg, fg=COLORS["muted"],
                 font=fonts.small, anchor="w").pack(fill="x", pady=(12, 0))
        self.repo_var = tk.StringVar(top)
        # A combo box: the list (when it could be fetched) is a help; typing a
        # name or pasting a link always works.
        self.entry = ttk.Combobox(body, textvariable=self.repo_var, width=46, values=())
        self.entry.pack(fill="x", pady=(2, 0))
        self.repo_var.trace_add("write", lambda *_a: self._on_input())
        self.repo_note = tk.Label(body, text="", bg=bg, fg=COLORS["faint"], font=fonts.small,
                                  anchor="w", justify="left", wraplength=420)

        tk.Label(body, text="Saves to", bg=bg, fg=COLORS["muted"],
                 font=fonts.small, anchor="w").pack(fill="x", pady=(10, 0))
        row = tk.Frame(body, bg=bg)
        row.pack(fill="x", pady=(2, 0))
        self.folder_btn = ttk.Button(row, text="Change...", style="Slim.TButton",
                                     command=self.pick_folder)
        self.folder_btn.pack(side="right", padx=(8, 0))
        self.preview = tk.Label(row, text="", bg=bg, fg=COLORS["text"],
                                font=fonts.mono, anchor="w", justify="left",
                                wraplength=340)
        self.preview.pack(side="left", fill="x", expand=True)
        Tooltip(self.preview, self.target_path).attach(self.preview)

        self.busy = tk.Label(body, text="", bg=bg, fg=COLORS["text"],
                             font=fonts.small_bold, anchor="w")
        self.error = tk.Frame(body, bg=bg)
        self.buttons = tk.Frame(body, bg=bg)
        self.buttons.pack(fill="x", pady=(14, 0))
        self.cancel_btn = ttk.Button(self.buttons, text="Cancel", command=self.cancel)
        self.cancel_btn.pack(side="right")
        self.go_btn = ttk.Button(self.buttons, text="Clone", command=self.clone)
        self.go_btn.pack(side="right", padx=(0, 8))

        top.bind("<Return>", lambda _e: self.clone())
        top.bind("<Escape>", lambda _e: None if self.running else self.cancel())
        self._on_input()
        self.entry.focus_set()
        _center_on(parent, top)
        _grab_modal(top)
        # The organisation's repositories, in the background (never required).
        self._repo_lister = repo_lister or (lambda: core.github_list_org_repos(org))
        run_in_background(top, self._repo_lister, self._show_repos)

    # -- state -------------------------------------------------------------

    def repo_name(self):
        return core.clone_target(self.org, self.repo_var.get())[1]

    def _show_repos(self, result):
        """The repository list arrived (or did not: then there is simply no list)."""
        if not self.alive or not result:
            return
        names = [r["name"] for r in (result.get("repos") or [])]
        try:
            self.entry.configure(values=names)
            if result.get("status") in ("empty", "rejected", "no_org") and result.get("message"):
                self.repo_note.configure(text=result["message"])
                self.repo_note.pack(fill="x", pady=(4, 0), after=self.entry)
        except tk.TclError:
            pass

    def target_path(self):
        """The folder the clone will create ("" until a parent folder is known)."""
        parent = self.dest.rstrip("/\\") if self.dest else ""
        return ("%s%s%s" % (parent, os.sep, self.repo_name() or "<name>")) if parent else ""

    def _on_input(self):
        # A long path shows its END (the folder that will be created); the
        # whole path is the tooltip.
        full = self.target_path()
        shown = full if len(full) <= 50 else "\u2026" + full[-49:]
        self.preview.configure(text=shown or "Choose a folder")
        if not self.running:
            ready = bool(self.dest and self.repo_name())
            self.go_btn.configure(state="normal" if ready else "disabled")

    def pick_folder(self):
        name = self.repo_name()
        path = filedialog.askdirectory(
            parent=self.top,
            title=("Choose where to create the '%s' folder" % name) if name
            else "Choose where to create the cloned study folder",
            initialdir=self.dest or core.clone_parent_default() or None)
        if path:
            self.dest = os.path.normpath(path)
            self.error.pack_forget()
        self._on_input()

    def _set_busy(self, on, text=""):
        self.running = bool(on)
        state = "disabled" if on else "normal"
        for widget in (self.entry, self.folder_btn, self.cancel_btn):
            widget.configure(state=state)
        if on:
            self.go_btn.configure(state="disabled", text="Cloning...")
            self.error.pack_forget()
            self.busy.configure(text=text)
            self.busy.pack(fill="x", pady=(10, 0), before=self.buttons)
        else:
            self.busy.pack_forget()
            self._on_input()

    def _phase(self, phase):
        if not self.alive:
            return
        target = "%s/%s" % core.clone_target(self.org, self.repo_var.get())
        self.busy.configure(text=("Cloning %s ..." % target) if phase == "cloning"
                            else ("Checking that %s exists ..." % target))

    def _show_error(self, result):
        for child in list(self.error.winfo_children()):
            child.destroy()
        bg = COLORS["card"]
        # BOLD = what to do; then what happened.
        action = result.get("action") or ""
        message = result.get("message") or "Could not clone."
        self.error_action = tk.Label(
            self.error, text=action or message, bg=bg, fg=COLORS["error"],
            font=self.fonts.small_bold, anchor="w", justify="left", wraplength=420)
        self.error_action.pack(fill="x")
        quiet = message if action else result.get("hint")
        if quiet:
            tk.Label(self.error, text=quiet, bg=bg, fg=COLORS["muted"],
                     font=self.fonts.small, anchor="w", justify="left",
                     wraplength=420).pack(fill="x", pady=(2, 0))
        self.use_existing_btn = None
        if result.get("status") == "exists" and result.get("existing_path"):
            row = tk.Frame(self.error, bg=bg)
            row.pack(fill="x", pady=(6, 0))
            existing = result["existing_path"]
            self.use_existing_btn = ttk.Button(
                row, text="Use that folder", style="Slim.TButton",
                command=lambda: self.use_existing(existing))
            self.use_existing_btn.pack(side="left")
            ttk.Button(row, text="Choose another folder...", style="Slim.TButton",
                       command=self.pick_folder).pack(side="left", padx=(8, 0))
        # GitHub answers "not found" for a repo this login cannot see, so both a
        # not-found and a login failure offer the GitHub login dialog.
        if result.get("status") in ("not_found", "auth"):
            ttk.Button(self.error, text=core.GITHUB_LOGIN_ACTION_LABEL,
                       command=self.open_login).pack(anchor="w", pady=(6, 0))
        if result.get("output"):
            fold = Collapsible(self.error, "Git output", self.fonts.small)
            fold.pack(fill="x", pady=(4, 0))
            _git_output_box(fold.inner, self.fonts, result["output"]).pack(fill="x")
        self.error.pack(fill="x", pady=(10, 0), before=self.buttons)
        self.go_btn.configure(text="Retry")
        self.entry.focus_set()
        self.entry.selection_range(0, "end")

    def use_existing(self, path):
        """The study is already on this computer: select that folder."""
        self._close()
        if callable(self.on_use_existing):
            self.on_use_existing(path)

    def open_login(self):
        GithubLoginDialog(self.top, self.fonts, on_done=self._login_saved, retry=True,
                          org=self.org)

    def _login_saved(self, ok, message, saved=False):
        """After the login dialog: say so in place of the error and, when a login
        was STORED, retry the clone at once (Save and retry)."""
        if not self.alive:
            return
        for child in list(self.error.winfo_children()):
            child.destroy()
        tk.Label(self.error, text=message, bg=COLORS["card"],
                 fg=COLORS["ok"] if ok else COLORS["error"], font=self.fonts.small_bold,
                 anchor="w", justify="left", wraplength=420).pack(fill="x")
        self.error.pack(fill="x", pady=(10, 0), before=self.buttons)
        self.go_btn.configure(text="Retry")
        self._on_input()
        if ok and saved:
            self.clone()

    # -- actions -----------------------------------------------------------

    def clone(self):
        if self.running:
            return
        typed = self.repo_var.get().strip()
        repo = self.repo_name()
        if not repo or not self.dest:
            return
        self._set_busy(True, "Checking that %s/%s exists ..."
                       % core.clone_target(self.org, typed))
        self.on_start()
        # Post back through the (always-alive) parent, not the dialog, so a
        # dialog closed mid-clone still gets its result delivered.
        org, dest, root = self.org, self.dest, self.parent

        def worker():
            result = core.git_clone_org_repo(
                org, typed, dest,
                on_phase=lambda phase: root.after(0, self._phase, phase))
            root.after(0, self._done, result)

        self._spawn(worker)

    @staticmethod
    def _spawn(target):
        """Run the clone off the UI thread (a seam tests replace)."""
        threading.Thread(target=target, name="git-clone", daemon=True).start()

    def _done(self, result):
        self.running = False
        if result.get("ok") and result.get("path"):
            self._close()
            self.on_finish(result, False)
            return
        if not self.alive:
            self.on_finish(result, False)
            return
        self._set_busy(False)
        self._show_error(result)
        self.on_finish(result, True)

    def cancel(self):
        # Closing mid-clone is allowed (window X; Cancel/Escape are disabled while
        # busy): the clone finishes in the background and on_finish reports it.
        self._close()

    def _close(self):
        self.alive = False
        try:
            self.top.destroy()
        except tk.TclError:
            pass


class GithubLoginDialog(object):
    """THE GitHub login dialog (Lab Settings > GitHub, a failed clone, a failed
    Git Pull, the "could not check" line).

    Username + token go through core.github_save_login_checked on a worker
    thread: core asks GitHub ONCE whether it accepts the token (a rejected one is
    NOT stored), then hands the login to the SYSTEM credential store. Nothing
    secret is written to a launcher file or the activity log, and the token
    field is cleared as soon as it has been sent. Where the token is stored is
    behind the info tip; "Create the token on GitHub" opens GitHub's own form,
    prefilled. "Forget GitHub login" asks first (it stops cloning for everybody
    on a shared PC). ``on_done(ok, message, saved)`` gets the outcome on the UI
    thread after the dialog closed on success; ``saved`` is True when a login
    was stored (the opener retries what failed: with ``retry`` the button reads
    "Save and retry"). It opens on top of another modal and gives the grab back
    to it when it closes.
    """

    def __init__(self, parent, fonts, on_done=None, spawn=None, retry=False, org=""):
        self.parent = parent
        self.fonts = fonts
        self.on_done = on_done
        self._spawn = spawn or (lambda target: threading.Thread(
            target=target, name="github-login", daemon=True).start())
        self.busy = False
        top = self.top = tk.Toplevel(parent)
        top.title(core.GITHUB_LOGIN_DIALOG_TITLE)
        top.configure(bg=COLORS["card"])
        top.transient(parent)
        top.resizable(False, False)
        top.protocol("WM_DELETE_WINDOW", self.close)
        bg = COLORS["card"]
        body = tk.Frame(top, bg=bg)
        body.pack(fill="both", expand=True, padx=18, pady=16)
        head = tk.Frame(body, bg=bg)
        head.pack(fill="x")
        tk.Label(head, text=core.GITHUB_LOGIN_DIALOG_TITLE, bg=bg, fg=COLORS["text"],
                 font=fonts.bold, anchor="w").pack(side="left")
        note = " ".join(x for x in (core.GITHUB_LOGIN_DIALOG_NOTE,
                                    core.github_platform_note()) if x)
        self.tip = info_tip(head, note, font=fonts.small)
        self.tip.pack(side="left")
        tk.Label(body, text="Enter the GitHub username and a token, then Save.", bg=bg,
                 fg=COLORS["text"], font=fonts.small_bold, anchor="w").pack(
            fill="x", pady=(6, 0))
        tk.Label(body, text="GitHub username", bg=bg, fg=COLORS["muted"],
                 font=fonts.small, anchor="w").pack(fill="x", pady=(12, 0))
        self.user_var = tk.StringVar(top)
        self.user_entry = ttk.Entry(body, textvariable=self.user_var, width=44)
        self.user_entry.pack(fill="x", pady=(2, 0))
        tk.Label(body, text="Token", bg=bg, fg=COLORS["muted"], font=fonts.small,
                 anchor="w").pack(fill="x", pady=(8, 0))
        row = tk.Frame(body, bg=bg)
        row.pack(fill="x", pady=(2, 0))
        self.token_var = tk.StringVar(top)
        self.token_entry = ttk.Entry(row, textvariable=self.token_var, show=MASK_CHAR)
        self.token_entry.pack(side="left", fill="x", expand=True)
        self.show_token = tk.BooleanVar(top, value=False)
        ttk.Checkbutton(row, text="Show", variable=self.show_token,
                        command=lambda: self.token_entry.configure(
                            show="" if self.show_token.get() else MASK_CHAR)).pack(
            side="left", padx=(6, 0))
        # GitHub's own "new token" form, prefilled for a read-only lab token.
        self.token_url = core.github_token_create_url(org)
        self.token_link = tk.Label(body, text=core.GITHUB_TOKEN_LINK_LABEL, bg=bg,
                                   fg=COLORS["accent"], font=fonts.small, cursor="hand2",
                                   anchor="w")
        self.token_link.pack(fill="x", pady=(6, 0))
        self.token_link.bind("<Button-1>", lambda _e: self.open_token_page())
        self.message = tk.Label(body, text="", bg=bg, fg=COLORS["error"], font=fonts.small,
                                anchor="w", justify="left", wraplength=420)
        self.message.pack(fill="x", pady=(8, 0))
        buttons = tk.Frame(body, bg=bg)
        buttons.pack(fill="x", pady=(10, 0))
        self.forget_btn = ttk.Button(buttons, text=core.GITHUB_FORGET_LABEL,
                                     command=self.forget)
        self.forget_btn.pack(side="left")
        self.save_btn = ttk.Button(
            buttons, text=core.GITHUB_LOGIN_SAVE_RETRY_LABEL if retry else "Save",
            command=self.save)
        self.save_btn.pack(side="right")
        ttk.Button(buttons, text="Cancel", command=self.close).pack(side="right", padx=(0, 8))
        top.bind("<Return>", lambda _e: self.save())
        top.bind("<Escape>", lambda _e: self.close())
        self.user_entry.focus_set()
        _center_on(parent, top)
        _grab_modal(top)

    def open_token_page(self):
        """Open GitHub's "new fine-grained token" form in the browser, prefilled."""
        webbrowser.open(self.token_url)

    def _set_busy(self, on):
        self.busy = bool(on)
        state = "disabled" if on else "normal"
        for widget in (self.save_btn, self.forget_btn):
            widget.configure(state=state)

    def save(self):
        if self.busy:
            return
        user = self.user_var.get().strip()
        token = self.token_var.get().strip()
        if not user or not token:
            self.message.configure(text="Enter the GitHub username and the token.",
                                   fg=COLORS["error"])
            return
        self.token_var.set("")   # sent; never kept in the dialog
        self._set_busy(True)
        self.message.configure(text="Checking ...", fg=COLORS["muted"])
        root = self.parent

        def worker():
            result = core.github_save_login_checked(user, token)
            root.after(0, self._finish, result, True)
        self._spawn(worker)

    def _confirm_forget(self):
        """Forget is destructive on a shared lab PC: name the consequence first."""
        return messagebox.askyesno(core.GITHUB_FORGET_LABEL, core.GITHUB_FORGET_CONFIRM,
                                   default="no", parent=self.top)

    def forget(self):
        if self.busy or not self._confirm_forget():
            return
        self._set_busy(True)
        root = self.parent

        def worker():
            result = core.github_forget_login_checked()
            root.after(0, self._finish, result, False)
        self._spawn(worker)

    def _finish(self, result, saved=False):
        self._set_busy(False)
        if not result.get("ok"):
            try:
                self.message.configure(text=result.get("message") or "Could not save.",
                                       fg=COLORS["error"])
            except tk.TclError:
                pass
            return
        self.close()
        if callable(self.on_done):
            _call_login_done(self.on_done, True, result.get("message", ""), saved)

    def close(self):
        self.token_var.set("")
        _modal_close(self.top)
        # Give the grab back to the dialog this one opened over.
        try:
            if isinstance(self.parent, tk.Toplevel) and self.parent.winfo_exists():
                self.parent.grab_set()
        except tk.TclError:
            pass


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
        head = tk.Frame(body, bg=COLORS["card"])
        head.pack(fill="x", pady=(0, 10))
        tk.Label(head, text="Save as new config",
                 bg=COLORS["card"], fg=COLORS["text"], font=fonts.bold, anchor="w").pack(side="left")
        info_tip(head, core.ui_tip("save_as"), font=fonts.small).pack(side="left")

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
    # A withdrawn (not-yet-mapped) window reports winfo_width/height as 1, so fall
    # back to the requested size. This lets a dialog be centered WHILE still
    # withdrawn and only then deiconified, so it never flashes at the default
    # position before jumping to centre.
    w = top.winfo_width()
    if w <= 1:
        w = top.winfo_reqwidth()
    h = top.winfo_height()
    if h <= 1:
        h = top.winfo_reqheight()
    x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - h) // divisor
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
                 on_open_dashboard=None, on_show_log=None):
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
        self.on_show_log = on_show_log
        self._issues = []
        self._fix_note = None
        self._launching = False
        # Preflight (psycopg2 connect, a settings.py import subprocess, a psycopg2
        # probe subprocess) is SLOW on a lab PC with the DB host down, so it runs
        # OFF the Tk thread: the modal opens at once in a "checking" state and the
        # issues are filled from a worker via _on_main (fable review item 8). A
        # monotonically-increasing token lets a newer render supersede a slow one.
        self._checking = False
        self._issues_token = 0
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
        # Shown as a PLAIN, normal modal: created mapped, transient to the root,
        # and grabbed exactly once after it is genuinely on screen. Deliberately
        # NO dim-shade overlay here (unlike some lighter dialogs). The shade is a
        # borderless grey Toplevel sized over the whole main window; if it ever
        # stacks above this heavy, canvas-based dialog it both greys the dialog
        # out and - with the grab held on the now-hidden dialog - swallows every
        # click, which is exactly the "grey ghost the shape of the main window
        # that grabs input, cannot be clicked and cannot be closed" regression.
        # A plain transient modal with no overlay cannot produce that. Likewise
        # no withdraw/deiconify + -topmost flash + focus_force dance (an earlier
        # revision of that left an unreachable grab on an invisible window).
        # Correctness beats polish here: a brief harmless grey flash is fine, a
        # locked app is not.
        top.title("Before you launch")
        top.configure(bg=COLORS["card"])
        top.transient(parent)
        top.resizable(False, False)

        body = self._body = tk.Frame(top, bg=COLORS["card"])
        body.pack(fill="both", expand=True, padx=20, pady=18)
        # A steady width, so the dialog does not jump as issues come and go.
        body.columnconfigure(0, weight=1, minsize=self.WRAP + 20)
        self._launched = False

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
        # Centre the fully-rendered window, then take the modal grab exactly once
        # and ONLY after the window is genuinely viewable. _wait_viewable is a
        # BOUNDED wait (it polls winfo_viewable with a deadline), so the grab is
        # never held while the window is invisible - which is what leaves an
        # unreachable "ghost" grab - and it can never hang the app if the window
        # somehow never maps. A single grab_set, no shade, no flashing.
        _center_on(parent, top)
        try:
            top.update_idletasks()
        except tk.TclError:
            pass
        _wait_viewable(top)
        try:
            top.grab_set()
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
            # An amber note, not a red block (#21): it appears on every launch on
            # the default database, and red is for things that block.
            bar = tk.Frame(self.summary_frame, bg=COLORS["warn_soft"], highlightthickness=1,
                           highlightbackground=COLORS["card_line"])
            bar.grid(row=r, column=0, sticky="ew", pady=(6, 8))
            bar.columnconfigure(1, weight=1)
            tk.Label(bar, text="▲", bg=COLORS["warn_soft"], fg=COLORS["warn"],
                     font=self.fonts.small_bold).grid(row=0, column=0, sticky="n",
                                                      padx=(10, 0), pady=6)
            self.caution_label = tk.Label(
                bar, text=briefing.get("caution_text", ""), bg=COLORS["warn_soft"],
                fg=COLORS["warn"], font=self.fonts.small, anchor="w", justify="left",
                wraplength=self.WRAP - 30)
            self.caution_label.grid(row=0, column=1, sticky="ew", padx=(7, 10), pady=6)
            r += 1
        tk.Label(self.summary_frame,
                 text="WHAT LAUNCHED" if getattr(self, "_launched", False) else "WHAT WILL LAUNCH",
                 bg=COLORS["card"],
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
            # The per-seat link and what welcome_page_ok=1 means: reference for
            # the lab manager, so it lives HERE behind the (i), not on the card
            # every launch (#1). Text from core so both launchers match.
            if not briefing.get("local_only"):
                link_text = (briefing.get("example_link", "") if briefing.get("open_room")
                             else briefing.get("link_template", ""))
                if link_text:
                    tk.Label(info, text="Link", bg=COLORS["field_off"], fg=COLORS["faint"],
                             font=fonts.small, anchor="nw", width=8).grid(
                        row=fr, column=0, sticky="nw", padx=(10, 0), pady=1)
                    tk.Label(info, text=link_text, bg=COLORS["field_off"],
                             fg=COLORS["muted"], font=fonts.mono_small, anchor="w",
                             justify="left", wraplength=self.WRAP - 120).grid(
                        row=fr, column=1, sticky="w", pady=1)
                    fr += 1
                welcome_note = briefing.get("welcome_note", "")
                if welcome_note:
                    self.welcome_label = tk.Label(
                        info, text=welcome_note, bg=COLORS["field_off"],
                        fg=COLORS["muted"], font=fonts.small, anchor="w", justify="left",
                        wraplength=self.WRAP - 50)
                    self.welcome_label.grid(row=fr, column=0, columnspan=2, sticky="ew",
                                            padx=10, pady=(5, 0))
                    fr += 1
            tk.Frame(info, bg=COLORS["field_off"], height=6).grid(row=fr, column=0)

        # The session's database is on another computer: say so plainly (grey).
        if briefing.get("db_host_note"):
            line = tk.Frame(summary, bg=COLORS["card"])
            line.grid(row=sr, column=0, sticky="w", pady=(0, 8))
            sr += 1
            tk.Label(line, text="Database: %s" % briefing.get("db_label", ""),
                     bg=COLORS["card"], fg=COLORS["muted"], font=fonts.small,
                     anchor="w").pack(side="left")
            tk.Label(line, text=briefing["db_host_note"], bg=COLORS["card"],
                     fg=COLORS["faint"], font=fonts.small, anchor="w").pack(
                side="left", padx=(6, 0))

        # 2. What to open on the participant computers. An open room has no seat
        # label, so its link is the plain room link.
        if briefing.get("open_room"):
            link_label = "Each participant computer opens"
            template = briefing.get("example_link", "")
        else:
            link_label = "Per-seat link (replace SEAT with the seat number)"
            template = briefing.get("link_template", "")
        if briefing.get("local_only"):
            # localhost: there are no participant computers, so no shortcut and no
            # link to hand out (#14). core supplies the one line.
            tk.Label(summary, text=briefing.get("local_only_text", ""), bg=COLORS["card"],
                     fg=COLORS["muted"], font=fonts.body, anchor="w").grid(
                row=sr, column=0, sticky="w", pady=(0, 8))
            sr += 1
        elif briefing.get("has_participant_links", False):
            # The study room: the shortcut is already on the PCs, so the link is
            # HIDDEN. It appears only while the pointer is over the chip (and a
            # click on the chip copies it).
            line = tk.Frame(summary, bg=COLORS["card"])
            line.grid(row=sr, column=0, sticky="w", pady=(0, 8))
            sr += 1
            tk.Label(line, text="Open", bg=COLORS["card"], fg=COLORS["muted"],
                     font=fonts.body).pack(side="left")
            chip = self.room_chip = tk.Label(
                line,
                text=briefing.get("shortcut_label") or briefing.get("shortcut_name", ""),
                bg=COLORS["accent_soft"],
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
            # (An "Other host" has no lab shortcuts at all, so there is no
            # study-room alternative to point at: it just gets the link.)
            if not briefing.get("is_study", False):
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

        # (The welcome_page_ok=1 explainer is behind the lab (i) above, with the
        # per-seat link it describes: it is reference, not something to read on
        # every launch.)

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
        """Show a "checking" placeholder immediately, then compute the (possibly
        slow) issues OFF the Tk thread and fill them in via _on_main, so the modal
        never freezes while core.preflight connects to the DB / imports settings.py
        on a slow lab PC (fable review item 8). Called again after an inline fix or
        a re-check; a stale worker's result is discarded via the token."""
        self._checking = True
        self._issues_token += 1
        token = self._issues_token
        for child in list(self.issues_frame.winfo_children()):
            child.destroy()
        tk.Label(self.issues_frame, text="Checking your setup…", bg=COLORS["card"],
                 fg=COLORS["muted"], font=self.fonts.small, anchor="w",
                 justify="left").grid(row=0, column=0, sticky="ew", pady=(2, 6))
        self._sync_issues_scroll()
        self._render_action()

        # The worker ONLY stores its result; the Tk main thread polls for it with
        # after() and does all the widget work, so no Tk call is ever made from
        # the worker thread (the RoomPickerDialog._start_enumerate pattern). A Tk
        # call from a non-main thread does not reliably run, which is why the
        # result must be handed back by polling, not by after() inside the worker.
        self._issues_result = None

        def work():
            try:
                issues = list(self.gather_issues())
            except Exception:
                issues = []
            self._issues_result = (token, issues)

        threading.Thread(target=work, name="preflight", daemon=True).start()
        self._poll_issues(token)

    def _poll_issues(self, token):
        """Poll (on the Tk thread) for the worker's issues, then fill them in."""
        if token != self._issues_token:
            return
        result = self._issues_result
        if result is not None and result[0] == token:
            self._issues_result = None
            self._fill_issues(result[1], token)
            return
        try:
            self.top.after(120, lambda: self._poll_issues(token))
        except tk.TclError:
            pass

    def _fill_issues(self, issues, token):
        """Render the issue cards computed by the worker, unless a newer render has
        superseded this one (token mismatch) or the window has closed."""
        if token != self._issues_token:
            return
        try:
            if not self.top.winfo_exists():
                return
        except tk.TclError:
            return
        self._checking = False
        for child in list(self.issues_frame.winfo_children()):
            child.destroy()
        self._issues = list(issues)
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
        # The bottom bar (Launch / Launch anyway / disabled) depends on the issue
        # counts we just filled in, so re-render it now the real issues are known.
        self._render_action()
        try:
            self.top.update_idletasks()
            _center_on(self.parent, self.top)
        except tk.TclError:
            pass

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
        # (its REAL width: a long label such as "Use this computer's default
        # database" used to cover the text) and for the scrollbar a tall list gets.
        fix_button = None
        fix_below = False
        if fix:
            fix_button = self._fix_button(frame, issue.get("fix_label", "Fix"),
                                          lambda f=fix: self._run_fix(f))
            # A long label goes UNDER the text (full-width text) instead of
            # squeezing the text into a narrow column beside it.
            fix_below = (fix_button.winfo_reqwidth() > 230
                         and not (is_room_pick or is_block_info))
            wrap = self.WRAP - (66 if fix_below else fix_button.winfo_reqwidth() + 76)
        else:
            wrap = self.WRAP - 66
        tk.Label(frame, text=issue.get("title", ""), bg=bg, fg=COLORS["text"],
                 font=self.fonts.bold if level == "block" else self.fonts.body,
                 anchor="w", justify="left", wraplength=wrap).grid(
            row=0, column=2, sticky="ew", padx=(7, 10), pady=(7, 2))
        hint = str(issue.get("hint", "")).strip()
        if hint:
            tk.Label(frame, text=hint, bg=bg, fg=COLORS["muted"], font=self.fonts.small,
                     anchor="w", justify="left", wraplength=wrap).grid(
                row=1, column=2, sticky="ew", padx=(7, 10), pady=(0, 2))
        if fix_button is not None and fix_below:
            fix_button.grid(row=2, column=2, sticky="w", padx=(7, 10), pady=(4, 0))
        elif fix_button is not None:
            fix_button.grid(row=0, column=3, rowspan=2, sticky="e", padx=(0, 10), pady=8)
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
        tk.Frame(self.action, bg=COLORS["card_line"], height=1).grid(
            row=0, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        # While the checks run off-thread, offer only Cancel and a disabled,
        # "Checking…" Launch so nobody launches before the setup has been verified
        # (fable review item 8).
        if self._checking:
            left = tk.Frame(self.action, bg=COLORS["card"])
            left.grid(row=1, column=0, sticky="w")
            ttk.Button(left, text="Cancel", command=self._close).pack(side="left")
            tk.Label(self.action, text="Checking your setup…", bg=COLORS["card"],
                     fg=COLORS["muted"], font=self.fonts.small,
                     anchor="e").grid(row=1, column=1, sticky="e", padx=(0, 10))
            tk.Button(self.action, text="Launch", state="disabled", relief="flat", bd=0,
                      font=self.fonts.bold, bg=COLORS["field_off"], fg=COLORS["faint"],
                      padx=16, pady=6, disabledforeground=COLORS["faint"],
                      highlightthickness=1, highlightbackground=COLORS["card_line"],
                      cursor="arrow").grid(row=1, column=2, sticky="e")
            self.top.bind("<Return>", lambda _e: None)
            return
        n_block = sum(1 for i in self._issues if i.get("level") == "block")
        n_warn = sum(1 for i in self._issues if i.get("level") == "warn")
        left = tk.Frame(self.action, bg=COLORS["card"])
        left.grid(row=1, column=0, sticky="w")
        ttk.Button(left, text="Cancel", command=self._close).pack(side="left")
        row_recheck = any(i.get("fix") and i.get("fix_label") == "Re-check"
                          for i in self._issues)
        if (n_block or n_warn) and not row_recheck:
            # Something fixed outside the launcher (database started, port freed)?
            # ONE Re-check: when an issue row carries its own, this one is left out.
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
            # The success line comes FIRST, above the summary (#22); on the
            # manual-login path the dashboard login box below is opened, since the
            # line tells the user to use it.
            if method == "cookie":
                lead = "Experiment launched. The dashboard opened already logged in."
            elif method == "manual":
                lead = ("Experiment launched. The dashboard opened at the login page: "
                        "log in with the username and password below.")
                self._creds_open = True
            else:
                lead = "Experiment launched, dashboard opening."
            tk.Label(line, text="✓", bg=COLORS["card"], fg=COLORS["ok"],
                     font=self.fonts.bold).pack(side="left", anchor="n")
            self.result_label = tk.Label(
                line, text=lead, bg=COLORS["card"], fg=COLORS["text"],
                font=self.fonts.small_bold, anchor="w", justify="left", wraplength=450)
            self.result_label.pack(side="left", padx=(6, 0))
            self._launched = True
            body = getattr(self, "_body", None)
            if body is not None:
                try:
                    self._issues_outer.grid_remove()
                    self.action.grid(row=0, column=0, sticky="ew", pady=(0, 8))
                    self._render_summary()
                    buttons = self.result_buttons = tk.Frame(body, bg=COLORS["card"])
                    buttons.grid(row=4, column=0, sticky="ew", pady=(2, 0))
                    buttons.columnconfigure(0, weight=1)
                    ttk.Button(buttons, text="Close", command=self._close).grid(
                        row=0, column=2, sticky="e")
                    self.open_dashboard_button = tk.Button(
                        buttons, text="Open dashboard", command=self._open_takeover_url,
                        font=self.fonts.bold, bg=COLORS["accent"], fg="#ffffff",
                        activebackground=COLORS["accent_dark"], activeforeground="#ffffff",
                        relief="flat", bd=0, padx=16, pady=6, cursor="hand2")
                    self.open_dashboard_button.grid(row=0, column=1, sticky="e", padx=(0, 8))
                    self.top.update_idletasks()
                    _center_on(self.parent, self.top)
                except (tk.TclError, AttributeError):
                    pass
        else:
            try:
                self.top.title("Launch failed")
            except tk.TclError:
                pass
            # The failure IS the screen now (#4): the "What will launch" summary,
            # the save link and any issue cards are hidden, so the bold takeaway
            # and the [See activity log] button are at the top, the reason
            # (minus its own "See the activity log.") right under them.
            for name in ("summary_frame", "save_frame", "_issues_outer"):
                frame = getattr(self, name, None)
                if frame is not None:
                    try:
                        frame.grid_remove()
                    except tk.TclError:
                        pass
            tk.Label(line, text="Launch failed.",
                     bg=COLORS["card"], fg=COLORS["error"],
                     font=self.fonts.small_bold, anchor="w", justify="left").pack(side="left")
            if self.on_show_log is not None:
                ttk.Button(line, text=core.ACTIVITY_LOG_BUTTON_LABEL,
                           command=self._show_log).pack(side="left", padx=(8, 0))
            reason = core.split_activity_log_hint(str(info or ""))[0]
            if reason and reason != "Launch failed.":
                tk.Label(self.action, text=reason, bg=COLORS["card"], fg=COLORS["muted"],
                         font=self.fonts.small, anchor="w", justify="left",
                         wraplength=460).grid(row=1, column=0, sticky="w", pady=(4, 0))
            try:
                self.top.update_idletasks()
                _center_on(self.parent, self.top)
            except (tk.TclError, AttributeError):
                pass

    def _show_log(self):
        """[See activity log]: the launch is over (it failed), so close this popup
        (it holds the grab) and reveal the log in the main window."""
        self._close()
        if callable(self.on_show_log):
            self.on_show_log()

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
    """Pick the database for this config: oTree's own SQLite and every database
    of THIS computer (machine.json), the default one marked "(default)", each
    with its creator in grey on the title line (plus a short note with an info
    tip when a localhost database was made on another computer). The list is
    core.list_databases, so this picker and the
    web one show the same databases; selecting one applies it to the config
    through app.choose_database (which saves the database's id)."""

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

        head = tk.Frame(body, bg=COLORS["card"])
        head.pack(fill="x", pady=(0, 8))
        tk.Label(head, text="Choose a database", bg=COLORS["card"], fg=COLORS["text"],
                 font=fonts.bold, anchor="w").pack(side="left")
        info_tip(head, core.ui_tip("db_picker"), font=fonts.small).pack(side="left")

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
        outer = tk.Frame(parent, bg=COLORS["card"], cursor="hand2")
        outer.pack(fill="x", pady=2)
        row = tk.Frame(outer, bg=COLORS["card"], cursor="hand2")
        row.pack(fill="x")
        tk.Label(row, text=("●" if selected else " "), bg=COLORS["card"],
                 fg=COLORS["accent"] if selected else COLORS["card"],
                 font=self.fonts.small_bold, width=2).pack(side="left")
        tk.Label(row, text=entry["title"], bg=COLORS["card"], fg=COLORS["text"],
                 font=self.fonts.small_bold if selected else self.fonts.body,
                 anchor="w").pack(side="left")
        if entry.get("is_default"):
            tk.Label(row, text=" (default)", bg=COLORS["card"], fg=COLORS["faint"],
                     font=self.fonts.small, anchor="w").pack(side="left")
        note = core.database_host_note(entry)
        if note:
            # Another computer's database: say where, in grey.
            tk.Label(row, text="  " + note, bg=COLORS["card"], fg=COLORS["faint"],
                     font=self.fonts.small, anchor="w").pack(side="left")
        if entry.get("researcher"):
            # The creator researcher, in grey, on the SAME line (whose DB is this).
            tk.Label(row, text="   created by %s" % entry["researcher"], bg=COLORS["card"],
                     fg=COLORS["faint"], font=self.fonts.small, anchor="w").pack(side="left")
        widgets = [outer, row] + row.winfo_children()
        # A localhost database made on another computer: the SHORT note, with the
        # full sentence behind the info tip (the tip itself does not choose).
        short = core.database_location_note(entry)
        if short:
            wrow = tk.Frame(outer, bg=COLORS["card"], cursor="hand2")
            wrow.pack(fill="x", padx=(28, 0))
            note_label = tk.Label(wrow, text=short, bg=COLORS["card"], fg=COLORS["warn"],
                                  font=self.fonts.small, anchor="w", justify="left")
            note_label.pack(side="left")
            info_tip(wrow, core.database_location_warning(entry),
                     font=self.fonts.small).pack(side="left")
            widgets += [wrow, note_label]
        for widget in widgets:
            widget.bind("<Button-1>", lambda _e, e=entry: self._choose(e))

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
                 researchers=None, suggested_researcher="", from_settings=False,
                 on_done=None):
        self.app = app
        self.fonts = fonts
        self._created = False
        # Opened from Lab Settings (N3): the new database only joins this
        # computer's list; the config on screen is NOT switched to it and no
        # "Save this setup?" box follows. ``on_done`` lets Settings repaint.
        self.from_settings = bool(from_settings)
        self.on_done = on_done
        top = self.top = tk.Toplevel(parent)
        top.title("Create a new database")
        top.configure(bg=COLORS["card"])
        _attach_shade(parent, top)
        top.transient(parent)
        top.resizable(False, False)

        body = tk.Frame(top, bg=COLORS["card"])
        body.pack(fill="both", expand=True, padx=20, pady=18)
        body.columnconfigure(0, minsize=165)
        body.columnconfigure(1, weight=1, minsize=280)

        # Name + Researcher + Create is the whole dialog (#12). A user of its own,
        # its password and "already exists" sit behind the Advanced tick; the
        # explanation is behind the info tip on the title.
        head = tk.Frame(body, bg=COLORS["card"])
        head.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        tk.Label(head, text="Create a new Postgres database",
                 bg=COLORS["card"], fg=COLORS["text"], font=fonts.bold,
                 anchor="w").pack(side="left")
        info_tip(head, core.ui_tip("create_database"), font=fonts.small).pack(side="left")

        self.name = tk.StringVar(top, value=suggested_name)   # prefilled from the project folder
        self.user = tk.StringVar(top)
        self.password = tk.StringVar(top)
        self._row(body, 2, "Database name", ttk.Entry(body, textvariable=self.name))
        # Host + Port of the database: visible, greyed and read-only by default;
        # only the pen at the END of the host box makes them editable. Another
        # host than this computer forces register-only (_on_host_change).
        admin = core.pg_admin_from_store(self.app.store_extra)
        # Localhost-only (lab setting, default on): localhost and NO pen.
        defaults = core.new_database_host_defaults(admin)
        style = ttk.Style(top)
        style.map("HostLock.TEntry",
                  fieldbackground=[("readonly", COLORS["field_off"])],
                  foreground=[("readonly", COLORS["faint"])])
        self.host = tk.StringVar(top, value=defaults["host"])
        self.port = tk.StringVar(top, value=defaults["port"])
        hostframe = tk.Frame(body, bg=COLORS["card"])
        hostframe.columnconfigure(0, weight=1)
        self.host_entry = ttk.Entry(hostframe, textvariable=self.host,
                                    style="HostLock.TEntry", state="readonly")
        self.host_entry.grid(row=0, column=0, sticky="ew")
        self.host_pen = tk.Button(
            hostframe, text="\u270E", command=self._unlock_host, relief="flat", bd=0,
            highlightthickness=0, padx=2, pady=0, cursor="hand2", font=fonts.body,
            bg=COLORS["field_off"], fg=COLORS["accent"],
            activebackground=COLORS["field_off"], activeforeground=COLORS["accent"])
        if defaults["editable"]:
            self.host_pen.place(in_=self.host_entry, relx=1.0, x=-3, rely=0.5, anchor="e")
        tk.Label(hostframe, text="Port", bg=COLORS["card"], fg=COLORS["muted"],
                 font=fonts.small).grid(row=0, column=1, padx=(8, 4))
        self.port_entry = ttk.Entry(hostframe, textvariable=self.port, width=7,
                                    style="HostLock.TEntry", state="readonly")
        self.port_entry.grid(row=0, column=2)
        self._row(body, 3, "Host", hostframe)
        if not defaults["editable"]:
            # Why the host is greyed: behind the tip, not a grey line.
            self.host_tip = info_tip(hostframe, core.ui_tip("host_locked"), font=fonts.small)
            self.host_tip.grid(row=0, column=3, padx=(4, 0))
        # Researcher (required): whose database this is. Type a new name or pick
        # from the shared roster (the same list Save-as-new uses). Recorded in the
        # registry so every config's picker shows the creator.
        self.researcher = tk.StringVar(top, value=suggested_researcher)
        self.researcher_combo = ttk.Combobox(
            body, textvariable=self.researcher, values=list(researchers or []))
        self._row(body, 4, "Researcher (required)", self.researcher_combo)

        # Advanced: a user of its own (+ password) and "already exists". Closed
        # by default; opens by itself when a user is typed or the host is remote.
        self.advanced = tk.BooleanVar(top, value=False)
        self.advanced_check = tk.Checkbutton(
            body, text="Advanced: own user / already exists", variable=self.advanced,
            command=self._apply_advanced, bg=COLORS["card"], fg=COLORS["text"],
            activebackground=COLORS["card"], activeforeground=COLORS["text"],
            selectcolor=COLORS["card"], font=fonts.small, anchor="w", bd=0,
            highlightthickness=0, padx=0, cursor="hand2")
        self.advanced_check.grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))
        adv = self.adv = tk.Frame(body, bg=COLORS["card"])
        adv.columnconfigure(0, minsize=165)
        adv.columnconfigure(1, weight=1)
        adv.grid(row=6, column=0, columnspan=2, sticky="ew")
        self._row(adv, 0, "New user (optional)", ttk.Entry(adv, textvariable=self.user))
        # No user = the database is owned by, and connects as, the Postgres admin
        # login (a reference, not a copy); a user = its own role + password.
        self.admin_note = tk.Label(adv, text=core.ADMIN_LOGIN_NOTE, bg=COLORS["card"],
                                   fg=COLORS["faint"], font=fonts.small, anchor="w")
        # Password entry with the Show toggle inline to its right (aligned like
        # the main window's password rows); its hint is behind the info tip.
        pwframe = tk.Frame(adv, bg=COLORS["card"])
        pwframe.columnconfigure(0, weight=1)
        self.pw_entry = ttk.Entry(pwframe, textvariable=self.password, show=MASK_CHAR)
        self.pw_entry.grid(row=0, column=0, sticky="ew")
        self.show_pw = tk.BooleanVar(top, value=False)
        ttk.Checkbutton(pwframe, text="Show", variable=self.show_pw,
                        command=self._toggle_pw).grid(row=0, column=1, padx=(6, 0))
        info_tip(pwframe, core.ui_tip("pg_new_user_password"),
                 font=fonts.small).grid(row=0, column=2, padx=(2, 0))
        self._row(adv, 1, "New password", pwframe)
        self.pw_label = adv.grid_slaves(row=1, column=0)[0]
        self.pwframe = pwframe
        self.admin_note.grid(row=1, column=1, sticky="w", pady=3)

        # "Already exists" (Feature 4): register the connection in the global
        # registry WITHOUT running CREATE DATABASE. The entry is identical either
        # way, so it is selectable/editable afterward. When ticked the button reads
        # "Register" and the create is skipped.
        self.already_exists = tk.BooleanVar(top, value=False)
        exists_row = tk.Frame(adv, bg=COLORS["card"])
        exists_row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.exists_check = tk.Checkbutton(
            exists_row,
            text="The database already exists in Postgres (register without creating)",
            variable=self.already_exists, command=self._on_exists_toggle,
            bg=COLORS["card"], fg=COLORS["text"], activebackground=COLORS["card"],
            activeforeground=COLORS["text"], selectcolor=COLORS["accent"],
            font=fonts.small, anchor="w", bd=0, highlightthickness=0, padx=0,
            cursor="hand2", wraplength=420, justify="left")
        self.exists_check.pack(side="left")
        self.user.trace_add("write", lambda *_a: self._on_user())
        self._on_user()
        self._apply_advanced()

        # One short grey note, shown only while the host is not this computer.
        self.host_note = tk.Label(body, text="", bg=COLORS["card"], fg=COLORS["faint"],
                                  font=fonts.small, anchor="w", justify="left",
                                  wraplength=420)
        self.host_note.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        self.host_note.grid_remove()

        self.status = tk.Label(body, text="", bg=COLORS["card"], fg=COLORS["muted"],
                               font=fonts.small, anchor="w", justify="left", wraplength=420)
        self.status.grid(row=9, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        # A selectable copy of the last message, so an error can be copied out.
        self.detail = tk.Text(body, height=3, font=fonts.mono_small, wrap="word",
                              bg=COLORS["field_off"], fg=COLORS["text"], relief="flat",
                              highlightthickness=1, highlightbackground=COLORS["card_line"])
        self.detail.grid(row=10, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.detail.configure(state="disabled")
        self.detail.grid_remove()

        buttons = tk.Frame(body, bg=COLORS["card"])
        buttons.grid(row=11, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        buttons.columnconfigure(0, weight=1)
        # "Cancel" until a create succeeds, then "Close".
        self.cancel_button = ttk.Button(buttons, text="Cancel", command=self._close)
        self.cancel_button.grid(row=0, column=0, sticky="w")
        self.create_button = tk.Button(
            buttons, text="Create", command=self._create, font=fonts.bold,
            bg=COLORS["accent"], fg="#ffffff", activebackground=COLORS["accent_dark"],
            activeforeground="#ffffff", relief="flat", padx=14, pady=6, cursor="hand2")
        self.create_button.grid(row=0, column=1, sticky="e")

        # Another host than this computer = register only (checked now for the
        # default, which is the Lab Settings admin host, and on every edit).
        self._auto_exists = False
        self.host.trace_add("write", lambda *_a: self._on_host_change())
        self._on_host_change()

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

    def _unlock_host(self):
        """The pen: make Host AND Port editable (clicking the box does nothing)."""
        self.host_entry.configure(state="normal")
        self.port_entry.configure(state="normal")
        self.host_pen.place_forget()
        self.host_entry.focus_set()
        self.host_entry.select_range(0, "end")

    def _apply_advanced(self):
        """Show or hide the Advanced part (own user, password, already exists)."""
        if self.advanced.get():
            self.adv.grid()
        else:
            self.adv.grid_remove()

    def _open_advanced(self):
        if not self.advanced.get():
            self.advanced.set(True)
        self._apply_advanced()

    def _on_host_change(self):
        """A non-local host ticks + locks "already exists" (register, never
        create) with one grey note; back to a local host restores the default
        (create), unless the user had ticked the box themselves."""
        if not core.is_local_host(self.host.get()):
            self._open_advanced()      # the locked "already exists" tick is in there
        if core.is_local_host(self.host.get()):
            self.exists_check.configure(state="normal")
            if self._auto_exists:
                self.already_exists.set(False)
                self._auto_exists = False
            self.host_note.configure(text="")
            self.host_note.grid_remove()
        else:
            if not self.already_exists.get():
                self.already_exists.set(True)
                self._auto_exists = True
            self.exists_check.configure(state="disabled")
            self.host_note.configure(text=core.REMOTE_DB_NOTE)
            self.host_note.grid()
        self._on_exists_toggle()

    def _on_exists_toggle(self):
        self.create_button.configure(
            text="Register" if self.already_exists.get() else "Create")

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
        exists = self.already_exists.get()
        if exists:
            # Register-only: no CREATE DATABASE is run.
            self.create_button.configure(state="disabled", text="Registering...")
            self._set_status("Registering the existing database...", COLORS["muted"])
            target = self.app._run_register_existing_database
        else:
            self.create_button.configure(state="disabled", text="Creating...")
            self._set_status("Creating the database...", COLORS["muted"])
            target = self.app._run_create_database
        thread = threading.Thread(
            target=target,
            # No user = the admin login: never send a (hidden) password then.
            args=(name, self.user.get().strip(),
                  self.password.get() if self.user.get().strip() else "",
                  researcher, self._done),
            kwargs={"host": self.host.get().strip() or "localhost",
                    "port": self.port.get().strip() or "5432"},
            daemon=True)
        thread.start()

    def _done(self, result):
        self.create_button.configure(
            state="normal", text="Register" if self.already_exists.get() else "Create")
        if result.get("ok"):
            self._created = True
            self.cancel_button.configure(text="Close")
            # A remote register that could not connect is a success WITH an
            # advisory: show it in the warn colour, not the ok green. So is a
            # database whose user cannot log in (a blank password on a Postgres
            # that requires one): kept, with a clear warning.
            unreachable = (result.get("reachable") is False) or bool(result.get("warning"))
            self._set_status(result.get("message", "Created."),
                             COLORS["warn"] if unreachable else COLORS["ok"])
            if not self.from_settings:
                # Opened from the database picker: this study wants the new
                # database, so the config on screen switches to it. From Lab
                # Settings it only joins the list (N3).
                self.app.apply_created_database(result.get("fields", {}))
            self.app.log(result.get("message", "Database created."),
                         "warn" if unreachable else "ok")
            if result.get("warning"):
                # Stay open so the warning is read; Close then offers the save.
                self.cancel_button.configure(command=self._close_and_offer)
            else:
                self.top.after(500, self._close_and_offer)
        else:
            self._set_status(result.get("message", "Could not create the database."),
                             COLORS["error"])

    def _on_user(self):
        """Show the password row only when a user is typed; otherwise the grey
        "Uses the Postgres admin login" note. A typed user opens Advanced."""
        if self.user.get().strip():
            self._open_advanced()
            self.admin_note.grid_remove()
            self.pw_label.grid()
            self.pwframe.grid()
        else:
            self.pw_label.grid_remove()
            self.pwframe.grid_remove()
            self.admin_note.grid()

    def _close_and_offer(self):
        self._close()
        if self.from_settings:
            # Added from Lab Settings: stay there, just repaint its list.
            if callable(self.on_done):
                self.on_done()
            return
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


class EditDatabaseDialog(object):
    """Edit an EXISTING database entry from Lab Settings (Feature 2).

    Edits one of this computer's databases (machine.json): title (the nickname),
    researcher and the connection. Saving goes through core.edit_database (the id,
    and so every config referencing it, is kept), then re-publishes the live
    registry. No Postgres is touched -- this only records connection details."""

    def __init__(self, parent, fonts, app, entry, on_saved=None):
        self.app = app
        self.fonts = fonts
        self.entry = entry or {}
        self.on_saved = on_saved
        self.is_lab = False   # the old "lab shared" built-in is gone (1.5.0)
        top = self.top = tk.Toplevel(parent)
        top.title("Edit database")
        top.configure(bg=COLORS["card"])
        _attach_shade(parent, top)
        top.transient(parent)
        top.resizable(False, False)

        body = tk.Frame(top, bg=COLORS["card"])
        body.pack(fill="both", expand=True, padx=20, pady=18)
        body.columnconfigure(1, weight=1)

        heading = ("Edit the lab shared database" if self.is_lab
                   else "Edit “%s”" % (self.entry.get("title") or "database"))
        head = tk.Frame(body, bg=COLORS["card"])
        head.grid(row=0, column=0, columnspan=2, sticky="ew")
        tk.Label(head, text=heading, bg=COLORS["card"], fg=COLORS["text"],
                 font=fonts.bold, anchor="w").pack(side="left")
        info_tip(head, core.ui_tip("edit_database"), font=fonts.small).pack(side="left")
        # Only facts about THIS database stay visible: where it was created
        # (faint) and, in full, the warning when that was another computer.
        facts = tk.Frame(body, bg=COLORS["card"])
        facts.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(2, 10))
        created = core.database_created_on_line(self.entry)
        if created:
            tk.Label(facts, text=created, bg=COLORS["card"], fg=COLORS["faint"],
                     font=fonts.small, anchor="w", justify="left",
                     wraplength=420).pack(fill="x")
        location = core.database_location_warning(self.entry)
        if location:
            tk.Label(facts, text=location, bg=COLORS["card"], fg=COLORS["warn"],
                     font=fonts.small, anchor="w", justify="left",
                     wraplength=420).pack(fill="x", pady=(2, 0))

        self.vars = {}
        r = 2
        if not self.is_lab:
            self.vars["title"] = tk.StringVar(top, value=self.entry.get("title", ""))
            self._row(body, r, "Title", ttk.Entry(body, textvariable=self.vars["title"])); r += 1
            self.vars["researcher"] = tk.StringVar(top, value=self.entry.get("researcher", ""))
            roster = core.list_researchers(app.store_extra, app.presets)
            self._row(body, r, "Researcher",
                      ttk.Combobox(body, textvariable=self.vars["researcher"],
                                   values=list(roster))); r += 1
        self.vars["db_name"] = tk.StringVar(top, value=self.entry.get("db_name", ""))
        self._row(body, r, "Database name", ttk.Entry(body, textvariable=self.vars["db_name"])); r += 1
        # Blank user = connect as this PC's Postgres admin login (by reference,
        # never a copy of its password): the password row is hidden then.
        admin_login = bool(self.entry.get("uses_admin_login"))
        self.vars["db_user"] = tk.StringVar(
            top, value="" if admin_login else self.entry.get("db_user", ""))
        self._row(body, r, "User", ttk.Entry(body, textvariable=self.vars["db_user"])); r += 1
        self.admin_note = tk.Label(
            body, text=(core.ADMIN_LOGIN_MISSING if self.entry.get("login_missing")
                        else core.ADMIN_LOGIN_NOTE),
            bg=COLORS["card"], fg=(COLORS["warn"] if self.entry.get("login_missing")
                                   else COLORS["faint"]),
            font=fonts.small, anchor="w", justify="left", wraplength=360)
        self.vars["db_password"] = tk.StringVar(
            top, value="" if admin_login else self.entry.get("db_password", ""))
        pwframe = tk.Frame(body, bg=COLORS["card"])
        pwframe.columnconfigure(0, weight=1)
        self.pw_entry = ttk.Entry(pwframe, textvariable=self.vars["db_password"], show=MASK_CHAR)
        self.pw_entry.grid(row=0, column=0, sticky="ew")
        self.show_pw = tk.BooleanVar(top, value=False)
        ttk.Checkbutton(pwframe, text="Show", variable=self.show_pw,
                        command=self._toggle_pw).grid(row=0, column=1, padx=(6, 0))
        info_tip(pwframe, core.ui_tip("pg_blank_password"),
                 font=fonts.small).grid(row=0, column=2, padx=(2, 0))
        self._row(body, r, "Password", pwframe)
        self.pw_label = body.grid_slaves(row=r, column=0)[0]
        self.pwframe = pwframe
        self.admin_note.grid(row=r, column=1, sticky="w", pady=3)
        r += 1
        self.vars["db_user"].trace_add("write", lambda *_a: self._on_user())
        self._on_user()
        hostport = tk.Frame(body, bg=COLORS["card"])
        hostport.columnconfigure(0, weight=1)
        self.vars["db_host"] = tk.StringVar(top, value=self.entry.get("db_host", ""))
        # Localhost-only (lab setting, default on): the host cannot be changed here.
        self.host_entry = ttk.Entry(hostport, textvariable=self.vars["db_host"])
        if core.load_databases_localhost_only():
            self.host_entry.configure(state="readonly")
        self.host_entry.grid(row=0, column=0, sticky="ew")
        tk.Label(hostport, text="Port", bg=COLORS["card"], fg=COLORS["muted"]).grid(
            row=0, column=1, padx=(12, 8))
        self.vars["db_port"] = tk.StringVar(top, value=self.entry.get("db_port", ""))
        ttk.Entry(hostport, textvariable=self.vars["db_port"], width=8).grid(row=0, column=2, sticky="w")
        self._row(body, r, "Host", hostport); r += 1

        self.status = tk.Label(body, text="", bg=COLORS["card"], fg=COLORS["muted"],
                               font=fonts.small, anchor="w", justify="left", wraplength=420)
        self.status.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(6, 0)); r += 1

        buttons = tk.Frame(body, bg=COLORS["card"])
        buttons.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        buttons.columnconfigure(0, weight=1)
        ttk.Button(buttons, text="Cancel", command=self._close).grid(row=0, column=0, sticky="w")
        tk.Button(buttons, text="Save", command=self._save, font=fonts.bold,
                  bg=COLORS["accent"], fg="#ffffff", activebackground=COLORS["accent_dark"],
                  activeforeground="#ffffff", relief="flat", padx=14, pady=6,
                  cursor="hand2").grid(row=0, column=1, sticky="e")

        _center_on(parent, top)
        try:
            _grab_modal(top)
        except tk.TclError:
            pass

    def _row(self, body, r, label, widget):
        tk.Label(body, text=label, bg=COLORS["card"], fg=COLORS["muted"],
                 font=self.fonts.body, anchor="w").grid(row=r, column=0, sticky="w",
                                                        pady=3, padx=(0, 10))
        widget.grid(row=r, column=1, sticky="ew", pady=3)

    def _toggle_pw(self):
        self.pw_entry.configure(show="" if self.show_pw.get() else MASK_CHAR)

    def _on_user(self):
        """Password row with a user; the admin-login note without one."""
        if self.vars["db_user"].get().strip():
            self.admin_note.grid_remove()
            self.pw_label.grid()
            self.pwframe.grid()
        else:
            self.pw_label.grid_remove()
            self.pwframe.grid_remove()
            self.admin_note.grid()

    def _save(self):
        if not self.vars["db_name"].get().strip():
            self.status.configure(text="Enter a database name.", fg=COLORS["warn"])
            return
        updates = {key: var.get() for key, var in self.vars.items()}
        # Blank user = the admin login (core: uses_admin_login); no password kept.
        updates["db_user"] = updates["db_user"].strip()
        if not updates["db_user"]:
            updates["db_password"] = ""
        try:
            with self.app._store_lock:
                core.edit_database(self.app.store_extra, self.entry.get("id"), **updates)
                self.app._persist_store()
        except ValueError as error:
            self.status.configure(text=str(error), fg=COLORS["error"])
            return
        except OSError as error:
            self.status.configure(text="Could not save: %s" % error, fg=COLORS["error"])
            return
        # Editing the PC default database changes the live LAB_DB
        # (core.edit_database re-publishes it), so refresh the main window.
        refresh_defaults_from_core()
        try:
            self.app.refresh_previews()
        except (tk.TclError, AttributeError):
            pass
        if callable(self.on_saved):
            self.on_saved()
        self._close()

    def _close(self):
        _modal_close(self.top)


class RoomPickerDialog(object):
    """Pick the room to serve: ONE list, ONE button.

    The list shows this lab's default room first, preselected and highlighted,
    then the project's own rooms (read in a throwaway subprocess by core, off the
    UI thread). "Use this room" confirms the selected row (a double-click or
    Return does too); Cancel changes nothing. What the lab default room means is
    behind the info tip on the title. "Type a room name instead" reveals a text
    box, opened automatically when the project's rooms cannot be read. Selecting
    a room does not change the lab or the seats.
    """

    def __init__(self, parent, fonts, app):
        self.app = app
        self.fonts = fonts
        # This lab's default room (pinned + highlighted at the top), not a
        # hardcoded "study".
        self.lab_room = app._lab_room()
        self._room_names = [self.lab_room]   # real names, parallel to the rows
        top = self.top = tk.Toplevel(parent)
        top.title("Choose the room")
        top.configure(bg=COLORS["card"])
        _attach_shade(parent, top)
        top.transient(parent)
        top.resizable(False, False)

        body = tk.Frame(top, bg=COLORS["card"])
        body.pack(fill="both", expand=True, padx=20, pady=18)
        body.columnconfigure(0, weight=1, minsize=380)

        head = tk.Frame(body, bg=COLORS["card"])
        head.grid(row=0, column=0, sticky="ew")
        tk.Label(head, text="Choose the room to serve", bg=COLORS["card"], fg=COLORS["text"],
                 font=fonts.bold, anchor="w").pack(side="left")
        info_tip(head, core.ui_tip("room_picker"), font=fonts.small).pack(side="left")

        # The list is there from the start with the lab default preselected; the
        # project's own rooms are added below it once they have been read.
        self.listbox = tk.Listbox(body, height=6, font=fonts.body, exportselection=False,
                                  highlightthickness=1, highlightbackground=COLORS["card_line"])
        self.listbox.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.listbox.bind("<Double-Button-1>", lambda _e: self._use())
        self.listbox.bind("<Return>", lambda _e: self._use())
        self._fill([self.lab_room])

        self.note = tk.Label(body, text="", bg=COLORS["card"], fg=COLORS["muted"],
                             font=fonts.small, anchor="w", justify="left", wraplength=400)
        self.note.grid(row=2, column=0, sticky="ew", pady=(6, 0))

        self.free = tk.StringVar(top, value="")
        self.free_link = tk.Label(body, text="Type a room name instead", bg=COLORS["card"],
                                  fg=COLORS["accent"], font=fonts.small, anchor="w",
                                  cursor="hand2")
        self.free_link.grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.free_link.bind("<Button-1>", lambda _e: self._show_free())
        self.free_entry = ttk.Entry(body, textvariable=self.free)
        self.free_entry.grid(row=4, column=0, sticky="ew", pady=(4, 0))
        self.free_entry.grid_remove()
        self._free_shown = False

        buttons = tk.Frame(body, bg=COLORS["card"])
        buttons.grid(row=5, column=0, sticky="ew", pady=(14, 0))
        buttons.columnconfigure(0, weight=1)
        ttk.Button(buttons, text="Cancel", command=self._close).grid(row=0, column=0, sticky="w")
        self.pick_button = tk.Button(
            buttons, text="Use this room", command=self._use, font=fonts.bold,
            bg=COLORS["accent"], fg="#ffffff", activebackground=COLORS["accent_dark"],
            activeforeground="#ffffff", relief="flat", padx=14, pady=6, cursor="hand2")
        self.pick_button.grid(row=0, column=1, sticky="e")
        top.bind("<Escape>", lambda _e: self._close())
        _center_on(parent, top)
        try:
            _grab_modal(top)
        except tk.TclError:
            pass
        self._start_enumerate()

    def _room_display(self, name):
        """The room name, with the muted "(lab default)" tag on this lab's room."""
        label = name
        if name == self.lab_room:
            label += " (lab default)"
        return label

    def _fill(self, names):
        """(Re)fill the list; the lab default stays first and selected."""
        self.listbox.delete(0, "end")
        self._room_names = []
        for name in names:
            if name in self._room_names:
                continue
            self._room_names.append(name)
            idx = len(self._room_names) - 1
            self.listbox.insert("end", self._room_display(name))
            if core.room_has_participant_links(name, self.lab_room):
                self.listbox.itemconfig(idx, foreground=COLORS["accent"],
                                        selectforeground=COLORS["accent"])
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(0)     # the lab default is preselected

    def _show_free(self):
        """Reveal the "type a room name" box (a click, or rooms unreadable)."""
        self._free_shown = True
        self.free_link.configure(text="Room name:", fg=COLORS["muted"], cursor="arrow")
        self.free_link.unbind("<Button-1>")
        self.free_entry.grid()
        try:
            self.free_entry.focus_set()
        except tk.TclError:
            pass

    def _start_enumerate(self):
        """Read the project's rooms off the UI thread (core has an 8s timeout).

        The worker only stores the result; the main thread polls for it with
        after(), so no Tk call is ever made from the worker thread (the lab room
        was read on the main thread, in __init__, for the same reason).
        """
        self.note.configure(text="Reading this project's rooms…", fg=COLORS["muted"])
        project = self.app.var["project_path"].get()
        lab_room = self.lab_room
        self._enum_result = None

        def work():
            self._enum_result = core.enumerate_rooms_for_picker(project, lab_room)

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
        """Add the project's rooms under the lab default (still preselected)."""
        try:
            if not self.top.winfo_exists():
                return
        except tk.TclError:
            return
        if result.get("ok"):
            self._fill([self.lab_room] + list(result.get("rooms", [])))
            self.note.configure(text="", fg=COLORS["muted"])
        else:
            # The rooms could not be read: say why and open the text box, so a
            # name can be typed straight away.
            self._fill([self.lab_room])
            self.note.configure(
                text="Could not read this project's rooms (%s)." % result.get("error", ""),
                fg=COLORS["warn"])
            self._show_free()

    def _use(self):
        """The one primary action: a typed name wins when the text box is open
        and filled in, otherwise the selected row."""
        typed = self.free.get().strip() if self._free_shown else ""
        if typed:
            self._choose(typed)
        else:
            self._pick_from_list()

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


def _ask_shortcut_hotkey(parent, fonts):
    """A small modal asking for the OPTIONAL global shortcut key.

    Returns (proceed, key): proceed is False when the user cancelled, and key is
    the single character typed (blank = no hotkey, the default). One field only;
    what the hotkey does is behind the info tip (core.ui_tip("export_hotkey")).
    """
    result = {"proceed": False, "key": ""}
    top = tk.Toplevel(parent)
    top.title("Export participant-PC shortcuts")
    top.configure(bg=COLORS["card"])
    _attach_shade(parent, top)
    top.transient(parent)
    top.resizable(False, False)
    body = tk.Frame(top, bg=COLORS["card"])
    body.pack(fill="both", expand=True, padx=20, pady=18)
    head = tk.Frame(body, bg=COLORS["card"])
    head.pack(anchor="w")
    tk.Label(head, text="Global shortcut key (optional)", bg=COLORS["card"],
             fg=COLORS["muted"], font=fonts.body, anchor="w").pack(side="left")
    info_tip(head, core.ui_tip("export_hotkey"), font=fonts.small).pack(side="left")
    key = tk.StringVar(top, value="")
    keyrow = tk.Frame(body, bg=COLORS["card"])
    keyrow.pack(anchor="w", pady=(2, 0))
    ttk.Entry(keyrow, textvariable=key, width=6).pack(side="left")
    tk.Label(keyrow, text="e.g. S = Ctrl+Alt+S", bg=COLORS["card"], fg=COLORS["faint"],
             font=fonts.small).pack(side="left", padx=(8, 0))
    buttons = tk.Frame(body, bg=COLORS["card"])
    buttons.pack(fill="x", pady=(14, 0))
    buttons.columnconfigure(0, weight=1)

    def go():
        result["proceed"] = True
        result["key"] = key.get().strip()
        _modal_close(top)

    ttk.Button(buttons, text="Cancel", command=lambda: _modal_close(top)).grid(
        row=0, column=0, sticky="w")
    tk.Button(buttons, text="Choose folder…", command=go, font=fonts.bold,
              bg=COLORS["accent"], fg="#ffffff", activebackground=COLORS["accent_dark"],
              activeforeground="#ffffff", relief="flat", padx=14, pady=5,
              cursor="hand2").grid(row=0, column=1, sticky="e")
    _center_on(parent, top)
    try:
        _grab_modal(top)
    except tk.TclError:
        pass
    top.wait_window()
    return result["proceed"], result["key"]


def export_participant_shortcuts_dialog(parent, fonts, lab_name, host, seats,
                                        room=None, shortcut_label=""):
    """Ask for an optional hotkey + a destination folder and write the .lnk bundle.

    Shared by the Add/Edit-lab tick box and the Lab Settings "Export PC
    shortcuts" button, so both go through the same hotkey prompt + folder picker
    + core writer (core.export_participant_shortcuts). Always produces Windows
    .lnk files (the participant PCs are Windows) even when this launcher runs on
    a Mac. Returns the core result dict, or None when the user cancelled.
    """
    labels = core.parse_seat_list(seats)
    if not labels:
        messagebox.showwarning(
            "No seats", "This lab has no seats, so there is nothing to make "
            "shortcuts for.", parent=parent)
        return None
    proceed, hotkey_key = _ask_shortcut_hotkey(parent, fonts)
    if not proceed:
        return None
    dest = filedialog.askdirectory(
        parent=parent, title="Choose where to save the participant-PC shortcuts",
        initialdir=os.path.expanduser("~"))
    if not dest:
        return None
    result = core.export_participant_shortcuts(
        dest, lab_name, host, labels, room=room, shortcut_label=shortcut_label,
        hotkey_key=hotkey_key)
    if result.get("ok"):
        messagebox.showinfo("Shortcuts created", result["message"], parent=parent)
    else:
        messagebox.showerror("Could not create shortcuts",
                             result.get("message", "Unknown error."), parent=parent)
    return result


class LabPresetEditDialog(object):
    """Add or edit one lab preset (name, IP, seat list). Validation and the
    write both go through core; the caller passes on_save(name, ip, seats)."""

    def __init__(self, parent, fonts, title, preset, on_save):
        self.on_save = on_save
        self.parent = parent
        self.fonts = fonts
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

        # Per-lab default room (Pass 7) + optional shortcut label. The ℹ icon
        # carries the room tooltip.
        roomrow = tk.Frame(body, bg=COLORS["card"])
        roomrow.grid(row=6, column=0, sticky="ew", pady=(8, 0))
        head = tk.Frame(roomrow, bg=COLORS["card"])
        head.pack(anchor="w")
        tk.Label(head, text="Default oTree room", bg=COLORS["card"], fg=COLORS["muted"],
                 font=fonts.body).pack(side="left")
        info_tip(head, core.ROOM_HELP, font=fonts.body).pack(side="left")
        self.room = tk.StringVar(top, value=(preset or {}).get("default_room", "")
                                 or DEFAULT_ROOM_NAME)
        ttk.Entry(roomrow, textvariable=self.room, width=22).pack(anchor="w", pady=(2, 0))
        shortcut_head = tk.Frame(roomrow, bg=COLORS["card"])
        shortcut_head.pack(anchor="w", pady=(8, 0))
        tk.Label(shortcut_head, text="Shortcut label (optional)", bg=COLORS["card"],
                 fg=COLORS["muted"], font=fonts.body).pack(side="left")
        info_tip(shortcut_head, core.ui_tip("shortcut_label"), font=fonts.body).pack(side="left")
        self.shortcut = tk.StringVar(top, value=(preset or {}).get("shortcut_label", ""))
        ttk.Entry(roomrow, textvariable=self.shortcut, width=22).pack(anchor="w", pady=(2, 0))

        # Optional: on Save, also write a folder of per-seat Windows kiosk .lnk
        # shortcuts (one per participant PC). Ticking it prompts for a destination
        # folder after the lab is saved. The Edit view also has an on-demand
        # "Export PC shortcuts" button, so this is just the create-time shortcut.
        self.make_shortcuts = tk.BooleanVar(top, value=False)
        ttk.Checkbutton(roomrow, text="Create participant-PC shortcuts (a folder of "
                        "per-seat kiosk .lnk files)",
                        variable=self.make_shortcuts).pack(anchor="w", pady=(10, 0))

        # Optional column count so the plain-grid seat map matches the room shape.
        colrow = tk.Frame(body, bg=COLORS["card"])
        colrow.grid(row=7, column=0, sticky="w", pady=(8, 0))
        tk.Label(colrow, text="Columns in the room grid (optional)", bg=COLORS["card"],
                 fg=COLORS["muted"], font=fonts.body).pack(side="left", padx=(0, 8))
        self.cols = tk.StringVar(top, value=str((preset or {}).get("cols", "") or ""))
        ttk.Entry(colrow, textvariable=self.cols, width=5).pack(side="left")
        tk.Label(colrow, text="blank = auto", bg=COLORS["card"], fg=COLORS["faint"],
                 font=fonts.small).pack(side="left", padx=(8, 0))

        self.status = tk.Label(body, text="", bg=COLORS["card"], fg=COLORS["error"],
                               font=fonts.small, anchor="w", justify="left", wraplength=420)
        self.status.grid(row=8, column=0, sticky="ew", pady=(6, 0))

        buttons = tk.Frame(body, bg=COLORS["card"])
        buttons.grid(row=9, column=0, sticky="ew", pady=(12, 0))
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
                                   self.seats.get("1.0", "end"), cols,
                                   self.room.get(), self.shortcut.get())
        if not ok:
            self.status.configure(text=message)
            return
        # The lab saved. If asked, also write the per-seat kiosk .lnk bundle,
        # while the dialog is still up so the folder picker stacks above it.
        if self.make_shortcuts.get():
            export_participant_shortcuts_dialog(
                self.top, self.fonts, self.name.get(), self.ip.get(),
                core.parse_seat_list(self.seats.get("1.0", "end")),
                room=self.room.get(), shortcut_label=self.shortcut.get())
        self._close()

    def _close(self):
        _modal_close(self.top)


class LaunchHistoryDialog(object):
    """A read-only viewer for the launch history log (fable review F).

    Lists the most recent launches (newest first) from data/sessions.jsonl:
    time, config, lab/room, database, seats and outcome. It is a passive VIEWER
    -- it never manages or re-runs anything (mission: a launcher, not a data
    platform); the log is written fail-soft by core.record_session on each launch.
    """

    def __init__(self, parent, fonts):
        self.fonts = fonts
        top = self.top = tk.Toplevel(parent)
        top.title("Launch history")
        top.configure(bg=COLORS["window"])
        _attach_shade(parent, top)
        top.transient(parent)
        top.geometry("780x560")
        top.minsize(520, 420)

        header = tk.Frame(top, bg=COLORS["window"])
        header.pack(fill="x", padx=16, pady=(14, 4))
        tk.Label(header, text="Recent launches", bg=COLORS["window"], fg=COLORS["text"],
                 font=fonts.bold, anchor="w").pack(side="left")

        try:
            ttk.Style(top).configure(
                "History.Treeview",
                rowheight=int(fonts.body.metrics("linespace")) + 8)
        except tk.TclError:
            pass
        cols = ("time", "config", "labroom", "database", "version", "seats", "outcome")
        tree = self.tree = ttk.Treeview(top, columns=cols, show="headings",
                                        style="History.Treeview")
        for key, label, width, anchor in (
                ("time", "When", 130, "w"),
                ("config", "Config", 120, "w"),
                ("labroom", "Lab / room", 130, "w"),
                ("database", "Database", 150, "w"),
                ("version", "Study version", 150, "w"),
                ("seats", "Seats", 50, "center"),
                ("outcome", "Outcome", 70, "center")):
            tree.heading(key, text=label)
            tree.column(key, width=width, anchor=anchor)
        scroll = ttk.Scrollbar(top, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True, padx=(16, 0), pady=(4, 12))
        scroll.pack(side="left", fill="y", padx=(0, 8), pady=(4, 12))

        self.empty = tk.Label(top, text="", bg=COLORS["window"], fg=COLORS["muted"],
                              font=fonts.small)

        footer = tk.Frame(top, bg=COLORS["window"])
        footer.pack(fill="x", padx=16, pady=(0, 12))
        ttk.Button(footer, text="Close", command=top.destroy).pack(side="right")

        self._load()
        _center_on(parent, top, divisor=6)
        try:
            _grab_modal(top)
        except tk.TclError:
            pass

    def _load(self):
        entries = core.read_sessions(limit=200)
        if not entries:
            self.tree.pack_forget()
            self.empty.configure(
                text="No launches have been logged yet. Each launch adds a line here.")
            self.empty.pack(fill="both", expand=True, padx=16, pady=24)
            return
        for entry in entries:
            lab_room = "%s / %s" % (entry.get("lab", ""), entry.get("room", ""))
            outcome = "OK" if entry.get("outcome") == "ok" else "Failed"
            self.tree.insert("", "end", values=(
                self._fmt_time(entry.get("timestamp", "")),
                entry.get("config", "") or "(unsaved)",
                lab_room,
                entry.get("database", ""),
                core.study_version_label(entry.get("study_version")),
                entry.get("seats", ""),
                outcome))

    @staticmethod
    def _fmt_time(stamp):
        try:
            when = _dt.datetime.fromisoformat(stamp)
        except (TypeError, ValueError):
            return str(stamp)
        return when.strftime("%d %b %H:%M")


class SettingsCard(tk.Frame):
    """One Lab Settings section, COLLAPSED to a single line:

        Title (i)      one-line summary                         [Edit]

    Set-once things are summarised once configured; Edit opens the section's
    ``body`` in place (the button then reads Done) and the dialog closes the
    other sections. The explanation of the section is behind the info tip.
    """

    def __init__(self, master, fonts, title, tip="", on_toggle=None):
        tk.Frame.__init__(self, master, bg=COLORS["card"], highlightthickness=1,
                          highlightbackground=COLORS["card_line"])
        self.columnconfigure(0, weight=1)
        self._on_toggle = on_toggle
        self._open = False
        head = tk.Frame(self, bg=COLORS["card"])
        head.grid(row=0, column=0, sticky="ew", padx=12, pady=8)
        head.columnconfigure(2, weight=1)
        self.title_label = tk.Label(head, text=title, bg=COLORS["card"], fg=COLORS["text"],
                                    font=fonts.bold, anchor="w")
        self.title_label.grid(row=0, column=0, sticky="w")
        self.tip = None
        if tip:
            self.tip = info_tip(head, tip, font=fonts.small)
            self.tip.grid(row=0, column=1, sticky="w")
        self.summary_label = tk.Label(head, text="", bg=COLORS["card"], fg=COLORS["muted"],
                                      font=fonts.body, anchor="w", justify="left")
        self.summary_label.grid(row=0, column=2, sticky="ew", padx=(14, 8))
        self.toggle_button = ttk.Button(head, text="Edit", width=6, style="Slim.TButton",
                                        command=self.toggle)
        self.toggle_button.grid(row=0, column=3, sticky="e")
        self.body = tk.Frame(self, bg=COLORS["card"])
        self.body.columnconfigure(0, weight=1)

    def set_summary(self, text, warn=False):
        self.summary_label.configure(text=text,
                                     fg=COLORS["warn"] if warn else COLORS["muted"])

    @property
    def summary(self):
        return str(self.summary_label.cget("text"))

    @property
    def is_open(self):
        return self._open

    def set_open(self, value):
        self._open = bool(value)
        if self._open:
            self.body.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 10))
            self.toggle_button.configure(text="Done")
        else:
            self.body.grid_remove()
            self.toggle_button.configure(text="Edit")

    def toggle(self):
        self.set_open(not self._open)
        if callable(self._on_toggle):
            self._on_toggle(self)


class LabSettingsDialog(object):
    """The Lab Settings page. Every section is a SettingsCard collapsed to one
    summary line (core.pg_admin_summary / databases_summary / labs_summary) with
    Edit; only the section being edited is open. Order (same as the web face):
    This computer, Labs, Databases, Postgres admin, GitHub Organisation Sync,
    then the footer (Launch history, View settings.py block, version) and Close.
    Every change is saved through the app's store (core splits it over the
    files); preset add/edit/delete and show/hide all go through core so the
    guards (no clobber, keep a visible lab) are the single source of truth."""

    def __init__(self, parent, fonts, app):
        self.app = app
        self.fonts = fonts
        top = self.top = tk.Toplevel(parent)
        top.title("Lab Settings")
        top.configure(bg=COLORS["window"])
        _attach_shade(parent, top)
        top.transient(parent)
        top.geometry("720x600")
        top.minsize(620, 420)

        # Close sits at the bottom of the dialog (not inside a card), always in
        # view below the scrolling sections.
        bottom = tk.Frame(top, bg=COLORS["window"])
        bottom.pack(side="bottom", fill="x", padx=16, pady=(6, 12))
        ttk.Button(bottom, text="Close", command=self._close).pack(side="right")

        # Scrollable so an opened section can never push the rest off a short screen.
        scroll = ScrollFrame(top, COLORS["window"])
        scroll.pack(fill="both", expand=True)
        outer = tk.Frame(scroll.inner, bg=COLORS["window"])
        outer.pack(fill="both", expand=True, padx=16, pady=14)
        outer.columnconfigure(0, weight=1)

        self.cards = {}
        self._build_identity_card(outer, row=0)      # This computer
        self._build_labs_card(outer, row=1)          # Labs
        self._build_custom_db_card(outer, row=2)     # Databases (+ the default)
        self._build_admin_card(outer, row=3)         # Postgres admin
        # -- GitHub: one summary line + Edit, like the sections above --
        self._build_github_sync_card(outer, row=4)
        # -- Footer: Launch history, View settings.py block, version / update --
        self._build_footer(outer, row=5)

        self._reload_tree()
        self._refresh_summaries()
        # A section that is not set up yet starts OPEN, so what is missing shows.
        if not read_lab_marker():
            self.cards["identity"].set_open(True)
        elif not core.pg_admin_from_store(app.store_extra).get("admin_username"):
            self.cards["admin"].set_open(True)
        # Who is logged in to GitHub on this computer (a git credential lookup,
        # in the background): fills the GitHub summary.
        self._refresh_github(may_open=True)
        top.protocol("WM_DELETE_WINDOW", self._close)
        top.bind("<Escape>", lambda _e: self._close())
        _center_on(parent, top, divisor=6)
        try:
            _grab_modal(top)
        except tk.TclError:
            pass

    def _card(self, outer, row, key, title, tip):
        card = SettingsCard(outer, self.fonts, title, tip=core.ui_tip(tip),
                            on_toggle=self._card_toggled)
        card.grid(row=row, column=0, sticky="ew", pady=((0, 0) if row == 0 else (10, 0)))
        self.cards[key] = card
        return card

    def _card_toggled(self, opened):
        """Only the section being edited is open: opening one closes the rest."""
        if opened.is_open:
            for card in self.cards.values():
                if card is not opened and card.is_open:
                    card.set_open(False)
        self._refresh_summaries()

    def _refresh_summaries(self):
        """Repaint every collapsed summary line from the current store."""
        try:
            extra = self.app.store_extra
            presets = self._lab_presets()
            current = read_lab_marker()
            cur = core.find_lab_preset(current, presets) if current else None
            self.cards["identity"].set_summary(
                cur["name"] if cur is not None else "Not set yet", warn=cur is None)
            self.cards["labs"].set_summary(core.labs_summary(presets))
            self.cards["databases"].set_summary(core.databases_summary(
                core.known_databases_from_store(extra), core.default_database_id(extra)))
            admin = core.pg_admin_from_store(extra)
            self.cards["admin"].set_summary(core.pg_admin_summary(admin),
                                            warn=not admin.get("admin_username"))
        except (tk.TclError, KeyError):
            pass

    # -- Postgres admin (collapsed: user@host) ------------------------------

    def _build_admin_card(self, outer, row):
        fonts, app, top = self.fonts, self.app, self.top
        card = self._card(outer, row, "admin", "Postgres admin", "postgres_admin")
        admin_card = card.body
        admin_card.columnconfigure(1, weight=1)
        admin = core.pg_admin_from_store(app.store_extra)
        self.a_user = tk.StringVar(top, value=admin["admin_username"])
        self.a_pw = tk.StringVar(top, value=admin["admin_password"])
        self.a_host = tk.StringVar(top, value=admin["admin_host"])
        self.a_port = tk.StringVar(top, value=admin["admin_port"])
        a_user_entry = ttk.Entry(admin_card, textvariable=self.a_user)
        self._admin_row(admin_card, 0, "Admin username", a_user_entry)
        self.a_pw_entry = ttk.Entry(admin_card, textvariable=self.a_pw, show=MASK_CHAR)
        self._admin_row(admin_card, 1, "Admin password", self.a_pw_entry)
        self.show_pw = tk.BooleanVar(top, value=False)
        pw_tools = tk.Frame(admin_card, bg=COLORS["card"])
        pw_tools.grid(row=1, column=2, sticky="w")
        ttk.Checkbutton(pw_tools, text="Show", variable=self.show_pw,
                        command=self._toggle_admin_pw).pack(side="left")
        # May be left blank: behind the tip, not a grey line.
        info_tip(pw_tools, core.ui_tip("pg_blank_password"), font=fonts.small).pack(side="left")
        a_host_entry = ttk.Entry(admin_card, textvariable=self.a_host)
        self._admin_row(admin_card, 2, "Host", a_host_entry)
        a_port_entry = ttk.Entry(admin_card, textvariable=self.a_port)
        self._admin_row(admin_card, 3, "Port", a_port_entry)
        # The admin details save on their own (no button to forget): each field
        # saves when it loses focus, and again when the section or dialog closes.
        for _e in (a_user_entry, self.a_pw_entry, a_host_entry, a_port_entry):
            _e.bind("<FocusOut>", lambda _ev: self._save_admin())
        self.admin_status = tk.Label(admin_card, text="", bg=COLORS["card"],
                                     fg=COLORS["faint"], font=fonts.small, anchor="w")
        self.admin_status.grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 0))

    # -- GitHub card ---------------------------------------------------------

    def _build_github_sync_card(self, outer, row):
        """The GitHub section, collapsed like the others:

            GitHub (i)   demo-lab · login lab-account · token until 30 Sep 2027   [Edit]

        Inside: the organisation name (the WHOLE setting: a name shows the GitHub
        button, an empty field hides it; a lab setting in lab_info.json) and who
        is logged in on this computer, with the ONE "GitHub login…" button
        (Forget lives inside that dialog). The explanation is behind the tip."""
        fonts = self.fonts
        card = self._card(outer, row, "github", "GitHub", "github")
        body = card.body
        body.columnconfigure(1, weight=1)
        self._github_stored = None            # the stored login, once looked up
        on = self.app._clone_enabled()
        self.gh_org_var = tk.StringVar(
            self.top, value=str(getattr(self.app, "github_org", "") or "") if on else "")
        tk.Label(body, text="Organisation name", bg=COLORS["card"], fg=COLORS["text"],
                 font=fonts.body, anchor="w").grid(row=0, column=0, sticky="w",
                                                   pady=(0, 6))
        org_entry = self.gh_org_entry = ttk.Entry(body, textvariable=self.gh_org_var)
        org_entry.grid(row=0, column=1, sticky="ew", padx=(10, 0), pady=(0, 6))
        org_entry.bind("<FocusOut>", lambda _ev: self._save_github_sync())
        org_entry.bind("<Return>", lambda _ev: self._save_github_sync())
        self.gh_status = tk.Label(body, text="", bg=COLORS["card"], fg=COLORS["faint"],
                                  font=fonts.small, anchor="w")
        self.gh_status.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 6))
        tk.Label(body, text="Login", bg=COLORS["card"], fg=COLORS["text"],
                 font=fonts.body, anchor="w").grid(row=2, column=0, sticky="w")
        login_row = tk.Frame(body, bg=COLORS["card"])
        login_row.grid(row=2, column=1, sticky="ew", padx=(10, 0))
        self.gh_login_who = tk.Label(login_row, text="…", bg=COLORS["card"],
                                     fg=COLORS["text"], font=fonts.body, anchor="w")
        self.gh_login_who.pack(side="left")
        self.gh_login_btn = ttk.Button(login_row, text=core.GITHUB_LOGIN_ACTION_LABEL,
                                       style="Slim.TButton",
                                       command=self._open_github_login)
        self.gh_login_btn.pack(side="left", padx=(10, 0))
        self.gh_login_status = tk.Label(body, text="", bg=COLORS["card"],
                                        fg=COLORS["faint"], font=fonts.small, anchor="w",
                                        justify="left", wraplength=560)
        self.gh_login_status.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self._refresh_gh_status()

    def _open_github_login(self):
        GithubLoginDialog(self.top, self.fonts, on_done=self._github_login_done,
                          org=getattr(self.app, "github_org", ""))

    def _github_login_done(self, ok, message, saved=False):
        try:
            self.gh_login_status.configure(text=message,
                                           fg=COLORS["ok"] if ok else COLORS["error"])
        except tk.TclError:
            pass
        self.app.log(message, "ok" if ok else "err")
        self._refresh_github()

    def _save_github_sync(self):
        """Persist the organisation name (empty = off) and refresh the main
        screen's GitHub button."""
        stored = self.app._set_github_org(self.gh_org_var.get())
        self.gh_org_var.set(stored["org"] if stored["enabled"] else "")
        self._refresh_gh_status(saved=True)

    def _refresh_gh_status(self, saved=False):
        """The status line under the field + the collapsed summary line."""
        status = getattr(self, "gh_status", None)
        if status is None:
            return
        on = self.app._clone_enabled()
        try:
            if saved:
                status.config(text=("On for %s. Saved in the lab settings."
                                    % self.app.github_org) if on
                              else "Off. Saved in the lab settings.")
            stored = self._github_stored
            sync = {"enabled": self.app.github_sync_enabled, "org": self.app.github_org}
            info = core.github_login_info()
            summary = core.github_summary(sync, stored, info) if stored is not None \
                else (self.app.github_org if on else "Off")
            self.cards["github"].set_summary(
                summary, warn=bool(on and stored is not None and not stored.get("has_login")))
            if stored is not None:
                who = (stored.get("username") or "a login is stored") \
                    if stored.get("has_login") else "No login on this computer"
                self.gh_login_who.configure(text=who)
        except (tk.TclError, KeyError):
            pass

    def _refresh_github(self, may_open=False):
        """Ask git's credential store who is logged in (in the background), then
        repaint the summary. An organisation that is set but has no login on this
        computer is a to-do: with ``may_open`` the section opens itself when no
        other section is open."""
        lookup = getattr(self.app, "_github_login_lookup", None) or core.github_stored_login

        def done(stored):
            self._github_stored = stored or {"has_login": False, "username": ""}
            self._refresh_gh_status()
            try:
                if may_open and self.app._clone_enabled() \
                        and not self._github_stored.get("has_login") \
                        and not any(card.is_open for card in self.cards.values()):
                    self.cards["github"].set_open(True)
            except (tk.TclError, KeyError):
                pass

        run_in_background(self.top, lookup, done)

    # -- footer: Launch history link + version / update (reviews F + I) --------

    def _build_footer(self, outer, row):
        # Lab Settings keeps the read-only Launch history link (review F) and, at
        # the very bottom, the version + the update controls (review I). The
        # once-a-day check runs HERE on Settings open (and once in the background
        # after start, for the dot on the gear); "Check for updates" forces it.
        # A git install gets a one-click Update (core.git_pull, as on the web
        # face); a downloaded copy gets the link to GitHub.
        card = tk.Frame(outer, bg=COLORS["window"])
        card.grid(row=row, column=0, sticky="ew", pady=(14, 8))
        card.columnconfigure(0, weight=1)
        links = tk.Frame(card, bg=COLORS["window"])
        links.grid(row=0, column=0, sticky="w")
        history = tk.Label(links, text="Launch history", bg=COLORS["window"],
                           fg=COLORS["accent"], font=self.fonts.small, cursor="hand2",
                           anchor="w")
        history.pack(side="left")
        history.bind("<Button-1>", lambda _e: LaunchHistoryDialog(self.top, self.fonts))
        # The block viewer is a tool, not part of any one section: it sits here
        # next to Launch history.
        self.view_block_link = tk.Label(
            links, text="View settings.py block…", bg=COLORS["window"],
            fg=COLORS["accent"], font=self.fonts.small, cursor="hand2", anchor="w")
        self.view_block_link.pack(side="left", padx=(18, 0))
        self.view_block_link.bind("<Button-1>", lambda _e: self._view_block())

        # The version always shows, with the "Check for updates" link after it.
        version_row = tk.Frame(card, bg=COLORS["window"])
        version_row.grid(row=1, column=0, sticky="w", pady=(12, 0))
        tk.Label(version_row, text="%s version %s" % (APP_NAME, core.APP_VERSION),
                 bg=COLORS["window"], fg=COLORS["faint"], font=self.fonts.small,
                 anchor="w").pack(side="left")
        self.check_update_link = tk.Label(
            version_row, text=core.UPDATE_CHECK_LABEL, bg=COLORS["window"],
            fg=COLORS["accent"], font=self.fonts.small, cursor="hand2")
        self.check_update_link.pack(side="left", padx=(12, 0))
        self.check_update_link.bind("<Button-1>", lambda _e: self._check_update(force=True))
        self.update_result = tk.Label(version_row, text="", bg=COLORS["window"],
                                      fg=COLORS["faint"], font=self.fonts.small)
        self.update_result.pack(side="left", padx=(12, 0))

        # The "new version available" note + the way to get it appear only when a
        # newer release has been recorded. Hidden until the check says so.
        self._update_box = tk.Frame(card, bg=COLORS["window"])
        self._update_box.grid(row=2, column=0, sticky="ew")
        self._update_box.grid_remove()
        self._update_box.columnconfigure(0, weight=1)
        self._update_note = tk.Label(self._update_box, text=core.UPDATE_LABEL,
                                     bg=COLORS["window"], fg=COLORS["accent"],
                                     font=self.fonts.small_bold, anchor="w")
        self._update_note.grid(row=0, column=0, sticky="w", pady=(6, 2))
        # git install: ONE Update button (git pull, then "restart to activate").
        self.update_button = ttk.Button(self._update_box, text=core.UPDATE_BUTTON_LABEL,
                                        style="Slim.TButton", command=self._run_update)
        # A downloaded copy: the sentence with "GitHub" as a BLUE CLICKABLE link. A
        # read-only Text lets the link sit inline mid-sentence and wrap cleanly.
        text = self._update_text = tk.Text(
            self._update_box, height=2, wrap="word", bd=0, highlightthickness=0,
            bg=COLORS["window"], fg=COLORS["muted"], font=self.fonts.small,
            cursor="arrow", padx=0, pady=0)
        text.tag_configure("link", foreground="#2f6fed", underline=True)
        text.tag_bind("link", "<Enter>", lambda _e: text.configure(cursor="hand2"))
        text.tag_bind("link", "<Leave>", lambda _e: text.configure(cursor="arrow"))
        text.tag_bind("link", "<Button-1>",
                      lambda _e: webbrowser.open(core.REPO_URL))
        text.insert("end", "Download the app folder from ")
        text.insert("end", "GitHub", ("link",))
        text.insert("end", " and replace the app folder — keep your data.")
        text.configure(state="disabled")

        # Hooks tests replace: the check, "is this a git install", the pull.
        self._update_checker = getattr(self.app, "_settings_update_checker", None) \
            or core.check_for_update
        self._is_git_install = getattr(self.app, "_is_git_install", None) \
            or core.is_git_install
        self._update_puller = getattr(self.app, "_update_puller", None) or core.git_pull
        self._update_running = False
        self._check_update(force=False)

    def _check_update(self, force=False):
        """Run the fail-soft update check on a background thread (the main
        thread collects the result). ``force`` is the "Check for updates" link:
        it asks GitHub now and says what it found, also when there is nothing."""
        if force:
            try:
                self.update_result.configure(text="Checking…", fg=COLORS["faint"])
            except tk.TclError:
                pass

        def work():
            result = dict(self._update_checker(force=True) if force
                          else self._update_checker())
            result["git_install"] = bool(self._is_git_install()) \
                if result.get("update_available") else False
            return result

        run_in_background(self.top, work, lambda result: self._show_update(result, force))

    def _show_update(self, result, forced=False):
        result = result or {"update_available": False, "check_failed": True}
        try:
            if not self._update_box.winfo_exists():
                return
            if forced:
                if result.get("update_available"):
                    self.update_result.configure(text="")
                elif result.get("check_failed"):
                    self.update_result.configure(
                        text="Could not reach GitHub to check. Try again later.",
                        fg=COLORS["faint"])
                else:
                    self.update_result.configure(text="You are up to date.",
                                                 fg=COLORS["ok"])
            if not result.get("update_available"):
                self._update_box.grid_remove()
                return
            note = result.get("label") or core.UPDATE_LABEL
            remote = result.get("remote_version") or ""
            if remote:
                note = "%s (%s)" % (note, remote)
            self._update_note.configure(text=note)
            self._update_text.grid_forget()
            self.update_button.grid_forget()
            if result.get("git_install"):
                self.update_button.grid(row=1, column=0, sticky="w")
            else:
                self._update_text.grid(row=1, column=0, sticky="ew")
            self._update_box.grid()
            self.app._apply_update_badge(result)
        except tk.TclError:
            pass

    def _run_update(self, discard=None):
        """The Update button: ``git pull`` on the launcher's own folder
        (core.git_pull). NOTIFY-ONLY, like the web face: on success it says the
        update is downloaded and to restart; nothing is relaunched."""
        if self._update_running:
            return
        self._update_running = True
        try:
            self.update_button.configure(state="disabled", text="Updating…")
        except tk.TclError:
            pass
        puller = self._update_puller
        run_in_background(self.top, lambda: puller(discard=discard) if discard else puller(),
                          self._update_done)

    def _update_done(self, result):
        self._update_running = False
        result = result or {"ok": False, "message": "Update failed. Please try again."}
        try:
            self.update_button.configure(state="normal", text=core.UPDATE_BUTTON_LABEL)
        except tk.TclError:
            pass
        self.app.log(result.get("message") or "Update finished.",
                     "ok" if result.get("ok") else "err")
        if result.get("ok"):
            try:
                self.update_button.configure(state="disabled")
                self._update_note.configure(text=core.UPDATE_DONE_TITLE)
            except tk.TclError:
                pass
            self.app._apply_update_badge({"update_available": False})
            note = result.get("restored_note") or ""
            messagebox.showinfo(
                core.UPDATE_DONE_TITLE,
                "%s.\n\n%s%s" % (core.UPDATE_DONE_ACTION, core.UPDATE_DONE_NOTE,
                                  ("\n\n" + note) if note else ""),
                parent=self.top)
            return
        # Blocked only by the launcher's own changed files: ONE way out that
        # needs no git. Everything a lab owns is in data/ and local/, which git
        # ignores; core still keeps a copy of the files in data/retired/.
        files = result.get("files") or []
        if result.get("can_discard") and files:
            if messagebox.askyesno(core.UPDATE_DISCARD_LABEL,
                                   "%s\n\n%s" % (result.get("message") or "",
                                                 core.update_discard_confirm(files)),
                                   default="no", parent=self.top):
                self._run_update(discard=list(files))
            return
        messagebox.showerror("Update did not complete",
                             result.get("message") or "git pull failed.", parent=self.top)

    # -- which lab is this computer (item 8) --------------------------------

    def _build_identity_card(self, outer, row=0):
        """This computer's lab. Collapsed: the lab's name. Edit shows the list;
        choosing a lab applies it at once (no separate button)."""
        card = self._card(outer, row, "identity", "This computer", "this_computer")
        body = card.body
        self.identity_var = tk.StringVar(self.top, value="")
        self.identity_combo = ttk.Combobox(body, textvariable=self.identity_var,
                                           state="readonly", width=34)
        self.identity_combo.grid(row=0, column=0, sticky="w")
        self.identity_combo.bind("<<ComboboxSelected>>", lambda _e: self._apply_identity())
        self.identity_status = tk.Label(body, text="", bg=COLORS["card"], fg=COLORS["muted"],
                                        font=self.fonts.small, anchor="w", justify="left",
                                        wraplength=600)
        self.identity_status.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self._refresh_identity()

    def _refresh_identity(self):
        presets = [p for p in self._lab_presets() if not p.get("deleted")]
        self._identity_id_by_name = {p["name"]: p["id"] for p in presets}
        self.identity_combo.configure(values=[p["name"] for p in presets])
        current = read_lab_marker()
        cur_preset = core.find_lab_preset(current, presets) if current else None
        if cur_preset is not None:
            self.identity_var.set(cur_preset["name"])
            self.identity_status.configure(text="", fg=COLORS["muted"])
        else:
            self.identity_var.set("")
            self.identity_status.configure(
                text="Not set yet: pick this computer's lab.", fg=COLORS["warn"])

    def _apply_identity(self):
        """Choosing a lab in the list makes this computer that lab, at once."""
        name = self.identity_var.get()
        lab_id = getattr(self, "_identity_id_by_name", {}).get(name)
        if not lab_id:
            self.identity_status.configure(text="Pick a lab first.", fg=COLORS["warn"])
            return
        if lab_id == read_lab_marker():
            return
        self.app.set_machine_lab(lab_id)
        self._reload_tree()
        self._refresh_identity()
        # Say what else it did: every other lab is hidden on the main screen.
        self.identity_status.configure(text=core.lab_identity_status(name), fg=COLORS["ok"])
        self._refresh_summaries()

    def _admin_row(self, card, r, label, widget):
        tk.Label(card, text=label, bg=COLORS["card"], fg=COLORS["muted"], font=self.fonts.body,
                 anchor="w").grid(row=r, column=0, sticky="w", padx=(0, 8), pady=3)
        widget.grid(row=r, column=1, sticky="ew", pady=3, padx=(0, 6))

    def _toggle_admin_pw(self):
        self.a_pw_entry.configure(show="" if self.show_pw.get() else MASK_CHAR)

    def _save_admin(self):
        # Mutate + save under the app store lock (re-entrant) so it cannot race a
        # background worker serialising the store.
        with self.app._store_lock:
            self.app.store_extra["pg_admin"] = {
                "admin_username": self.a_user.get().strip(),
                "admin_password": self.a_pw.get(),
                "admin_host": self.a_host.get().strip(),
                "admin_port": self.a_port.get().strip(),
            }
            # Databases that use the admin login follow it live: republish.
            core.apply_default_database(self.app.store_extra)
            self.app._persist_store()
        self.admin_status.configure(text="Saved ✓", fg=COLORS["ok"])
        self._refresh_summaries()

    # -- database section: custom registry + default ------------------------

    def _build_custom_db_card(self, outer, row):
        """This computer's databases. Collapsed: "<default> (default) + N more".
        Edit shows the list: a radio per row picks this computer's DEFAULT
        database (the Lab default config uses it), Edit opens the entry, and
        "Add a database" adds one. The localhost-only lab setting is a tick box
        with its explanation behind an info tip."""
        card = self._card(outer, row, "databases", "Databases", "databases")
        body = card.body
        self.default_db = tk.StringVar(
            self.top, value=core.default_database_id(self.app.store_extra))
        self.db_list_frame = tk.Frame(body, bg=COLORS["card"])
        self.db_list_frame.grid(row=0, column=0, sticky="ew")
        self.db_list_frame.columnconfigure(0, weight=1)
        btns = tk.Frame(body, bg=COLORS["card"])
        btns.grid(row=1, column=0, sticky="ew", pady=(8, 4))
        ttk.Button(btns, text="Add a database", command=self._add_database).pack(side="left")
        # Lab setting (lab_info.json), default on: new and edited databases stay
        # on this computer. Off = another host can be registered (never created).
        self.db_localhost_only = tk.BooleanVar(
            self.top, value=core.load_databases_localhost_only())
        local_row = tk.Frame(body, bg=COLORS["card"])
        local_row.grid(row=2, column=0, sticky="w", pady=(6, 0))
        tk.Checkbutton(
            local_row, text=core.LOCALHOST_ONLY_LABEL, variable=self.db_localhost_only,
            command=self._save_db_localhost_only, bg=COLORS["card"], fg=COLORS["text"],
            activebackground=COLORS["card"], activeforeground=COLORS["text"],
            selectcolor=COLORS["card"], font=self.fonts.small_bold, anchor="w",
            bd=0, highlightthickness=0, cursor="hand2").pack(side="left")
        info_tip(local_row, core.ui_tip("localhost_only"), font=self.fonts.small).pack(side="left")
        self.db_local_status = tk.Label(body, text="", bg=COLORS["card"],
                                        fg=COLORS["muted"], font=self.fonts.small,
                                        anchor="w")
        self.db_local_status.grid(row=3, column=0, sticky="ew", pady=(2, 0))
        self._reload_db_list()

    def _save_db_localhost_only(self):
        stored = core.save_databases_localhost_only(self.db_localhost_only.get())
        self.db_localhost_only.set(stored)
        self.db_local_status.configure(text="Saved ✓", fg=COLORS["ok"])

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
            tk.Label(frame, text=("No databases on this computer yet: launches use "
                                  "oTree's own SQLite."), bg=COLORS["card"],
                     fg=COLORS["faint"], font=self.fonts.small, anchor="w").grid(
                row=0, column=0, sticky="ew", pady=2)
            return
        frame.columnconfigure(0, weight=1)
        default_id = core.default_database_id(self.app.store_extra)
        self.default_db.set(default_id)
        can_be_default = set(e["id"] for e in
                             core.default_database_options(self.app.store_extra))
        self.default_radios = {}
        for i, entry in enumerate(customs):
            block = tk.Frame(frame, bg=COLORS["card"])
            block.grid(row=i, column=0, sticky="ew", pady=1)
            line = tk.Frame(block, bg=COLORS["card"])
            line.pack(fill="x")
            # The radio makes this database the computer's DEFAULT (the one the
            # Lab default config uses): the old separate "Default database" card.
            if entry["id"] in can_be_default:
                radio = tk.Radiobutton(
                    line, variable=self.default_db, value=entry["id"],
                    command=self._save_default_db, bg=COLORS["card"],
                    activebackground=COLORS["card"], highlightthickness=0, bd=0,
                    cursor="hand2")
                radio.pack(side="left")
                Tooltip(radio, lambda: "Make this the default database of this "
                                       "computer").attach(radio)
                self.default_radios[entry["id"]] = radio
            tk.Label(line, text=entry["title"], bg=COLORS["card"], fg=COLORS["text"],
                     font=self.fonts.body, anchor="w").pack(side="left")
            if entry["id"] == default_id:
                tk.Label(line, text=" (default)", bg=COLORS["card"], fg=COLORS["faint"],
                         font=self.fonts.small, anchor="w").pack(side="left")
            # Only "on HOST:PORT" for another computer's database; nothing for
            # localhost. Where/when it was created is in the Edit dialog.
            note = core.database_host_note(entry)
            if note:
                tk.Label(line, text="  " + note, bg=COLORS["card"], fg=COLORS["faint"],
                         font=self.fonts.small, anchor="w").pack(side="left")
            if entry.get("researcher"):
                tk.Label(line, text="   created by %s" % entry["researcher"], bg=COLORS["card"],
                         fg=COLORS["faint"], font=self.fonts.small, anchor="w").pack(side="left")
            ttk.Button(line, text="Edit", width=6,
                       command=lambda e=entry: self._edit_database(e)).pack(side="right")
            # A localhost database made on another computer: the short note, with
            # the full sentence behind the info tip (and in the Edit dialog).
            short = core.database_location_note(entry)
            if short:
                wrow = tk.Frame(block, bg=COLORS["card"])
                wrow.pack(fill="x", padx=(24, 0))
                tk.Label(wrow, text=short, bg=COLORS["card"], fg=COLORS["warn"],
                         font=self.fonts.small, anchor="w", justify="left").pack(side="left")
                info_tip(wrow, core.database_location_warning(entry),
                         font=self.fonts.small).pack(side="left")
            if entry.get("login_missing"):
                tk.Label(block, text=core.ADMIN_LOGIN_MISSING, bg=COLORS["card"],
                         fg=COLORS["warn"], font=self.fonts.small, anchor="w",
                         justify="left", wraplength=560).pack(fill="x", padx=(24, 0))

    def _edit_database(self, entry):
        EditDatabaseDialog(self.top, self.fonts, self.app, entry,
                           on_saved=self._after_db_edit)

    def _after_db_edit(self):
        """Repaint the Databases section after an add / edit (the default's
        title/creds may have changed) and refresh the main window."""
        self._reload_db_list()
        self._refresh_summaries()
        try:
            self.app.refresh_previews()
        except (tk.TclError, AttributeError):
            pass

    def _add_database(self):
        # Persist any admin creds typed but not yet blurred, so the create flow
        # (which reads pg_admin) sees them. The new database only joins this
        # computer's list: the config on the main screen is NOT switched to it
        # (N3), and Settings stays open; the list repaints when the dialog closes.
        try:
            self._save_admin()
        except tk.TclError:
            pass
        dialog = self.app.create_database_dialog(from_settings=True, parent=self.top)
        try:
            dialog.top.bind(
                "<Destroy>",
                lambda e, t=dialog.top: self._after_db_edit() if e.widget is t else None,
                add="+")
        except (tk.TclError, AttributeError):
            pass

    def _save_default_db(self):
        """Make the chosen database this computer's default (by reference); the
        generated Lab default follows it at once."""
        with self.app._store_lock:
            try:
                core.set_default_database(self.app.store_extra, self.default_db.get())
            except ValueError:
                return
            self.app._persist_store()
        refresh_defaults_from_core()
        self._reload_db_list()
        self._refresh_summaries()
        try:
            self.app.refresh_previews()
        except (tk.TclError, AttributeError):
            pass

    def _view_block(self):
        self.app.show_block()

    # -- labs ---------------------------------------------------------------

    def _lab_presets(self):
        return core.lab_presets_from_store(self.app.store_extra)

    def _build_labs_card(self, outer, row):
        """The labs (lab_info.json). Collapsed: the shown labs by name. Edit
        shows one row per lab: a real "Shown" tick box (shown on the main
        screen or not), then Edit / Export PC shortcuts / Delete for that lab,
        and "Add lab" underneath."""
        card = self._card(outer, row, "labs", "Labs", "labs")
        body = card.body
        self.labs_frame = tk.Frame(body, bg=COLORS["card"])
        self.labs_frame.grid(row=0, column=0, sticky="ew")
        btns = tk.Frame(body, bg=COLORS["card"])
        btns.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(btns, text="Add lab", command=self._add).pack(side="left")
        self.preset_status = tk.Label(body, text="", bg=COLORS["card"],
                                      fg=COLORS["muted"], font=self.fonts.small, anchor="w",
                                      justify="left", wraplength=600)
        self.preset_status.grid(row=2, column=0, sticky="ew", pady=(4, 0))

    def _reload_tree(self):
        """(Re)build the lab rows (the name is kept from the old Treeview)."""
        frame = getattr(self, "labs_frame", None)
        if frame is None:
            return
        try:
            for child in list(frame.winfo_children()):
                child.destroy()
        except tk.TclError:
            return
        frame.columnconfigure(1, weight=1)
        for c, head in enumerate(("Shown", "Lab", "IP", "Seats")):
            tk.Label(frame, text=head, bg=COLORS["card"], fg=COLORS["faint"],
                     font=self.fonts.small, anchor="w").grid(
                row=0, column=c, sticky="w", padx=(0, 12))
        self.shown_vars = {}
        self.shown_checks = {}
        r = 1
        for preset in self._lab_presets():
            if preset.get("deleted"):
                continue   # soft-deleted: kept in lab_info.json, hidden here
            lab_id = preset["id"]
            var = tk.BooleanVar(self.top, value=bool(preset["display"]))
            self.shown_vars[lab_id] = var
            check = tk.Checkbutton(
                frame, variable=var, command=lambda i=lab_id: self._toggle_shown(i),
                bg=COLORS["card"], activebackground=COLORS["card"],
                selectcolor=COLORS["card"], highlightthickness=0, bd=0, cursor="hand2")
            check.grid(row=r, column=0, sticky="w")
            self.shown_checks[lab_id] = check
            tk.Label(frame, text=preset["name"], bg=COLORS["card"], fg=COLORS["text"],
                     font=self.fonts.body, anchor="w").grid(row=r, column=1, sticky="w",
                                                            padx=(0, 12))
            tk.Label(frame, text=preset["ip"], bg=COLORS["card"], fg=COLORS["muted"],
                     font=self.fonts.small, anchor="w").grid(row=r, column=2, sticky="w",
                                                             padx=(0, 12))
            tk.Label(frame, text=str(len(preset["seats"])), bg=COLORS["card"],
                     fg=COLORS["muted"], font=self.fonts.small, anchor="w").grid(
                row=r, column=3, sticky="w", padx=(0, 12))
            tools = tk.Frame(frame, bg=COLORS["card"])
            tools.grid(row=r, column=4, sticky="e", pady=1)
            ttk.Button(tools, text="Edit", width=6, style="Slim.TButton",
                       command=lambda i=lab_id: self._edit(i)).pack(side="left")
            ttk.Button(tools, text="Export PC shortcuts", style="Slim.TButton",
                       command=lambda i=lab_id: self._export_shortcuts(i)).pack(
                side="left", padx=(6, 0))
            ttk.Button(tools, text="Delete", width=7, style="Slim.TButton",
                       command=lambda i=lab_id: self._delete(i)).pack(side="left", padx=(6, 0))
            r += 1

    def _save_presets(self, new_list):
        with self.app._store_lock:
            self.app.store_extra["lab_presets"] = new_list
            self.app._persist_store()
        self._reload_tree()
        try:
            self._refresh_identity()
        except (tk.TclError, AttributeError):
            pass
        self.app.apply_lab_presets_change()
        self._refresh_summaries()

    def _add(self):
        def on_save(name, ip, seats, cols=0, default_room=None, shortcut_label=None):
            ok, message, new_list, _preset = core.add_lab_preset(
                self._lab_presets(), name, ip, seats, cols=cols,
                default_room=default_room, shortcut_label=shortcut_label)
            if ok:
                self._save_presets(new_list)
                self.preset_status.configure(text=message, fg=COLORS["ok"])
            return ok, message
        LabPresetEditDialog(self.top, self.fonts, "Add a lab", None, on_save)

    def _edit(self, lab_id):
        preset = core.find_lab_preset(lab_id, self._lab_presets())
        if preset is None:
            self.preset_status.configure(text="No lab with that id.", fg=COLORS["warn"])
            return

        def on_save(name, ip, seats, cols=0, default_room=None, shortcut_label=None):
            ok, message, new_list, _preset = core.update_lab_preset(
                self._lab_presets(), lab_id, name=name, ip=ip, seats=seats, cols=cols,
                default_room=default_room, shortcut_label=shortcut_label)
            if ok:
                self._save_presets(new_list)
                self.preset_status.configure(text=message, fg=COLORS["ok"])
            return ok, message
        LabPresetEditDialog(self.top, self.fonts, "Edit lab", preset, on_save)

    def _export_shortcuts(self, lab_id):
        """Regenerate the per-seat kiosk .lnk bundle for one lab."""
        preset = core.find_lab_preset(lab_id, self._lab_presets())
        if preset is None:
            self.preset_status.configure(text="No lab with that id.", fg=COLORS["warn"])
            return
        result = export_participant_shortcuts_dialog(
            self.top, self.fonts, preset["name"], preset["ip"], preset["seats"],
            room=preset.get("default_room"), shortcut_label=preset.get("shortcut_label", ""))
        if result and result.get("ok"):
            self.preset_status.configure(text=result["message"], fg=COLORS["ok"])
        elif result:
            self.preset_status.configure(
                text=result.get("message", "Could not create shortcuts."), fg=COLORS["warn"])

    def _toggle_shown(self, lab_id):
        """The "Shown" tick box: show or hide a lab on the main screen. core
        refuses to hide this computer's own lab (the tick is put back)."""
        preset = core.find_lab_preset(lab_id, self._lab_presets())
        if preset is None:
            return
        want = bool(self.shown_vars[lab_id].get())
        ok, message, new_list = core.set_lab_display(self._lab_presets(), lab_id, want)
        if ok:
            self._save_presets(new_list)
            self.preset_status.configure(text="", fg=COLORS["muted"])
        else:
            self.shown_vars[lab_id].set(bool(preset["display"]))
            self.preset_status.configure(text=message, fg=COLORS["warn"])

    def _delete(self, lab_id):
        preset = core.find_lab_preset(lab_id, self._lab_presets())
        if preset is None:
            return
        if not messagebox.askyesno(
                "Delete lab", "Delete the lab %r? It is hidden everywhere (kept in "
                "lab_info.json, so it can be restored by hand)." % preset["name"],
                parent=self.top):
            return
        current = self.app.var["lab"].get()
        ok, message, new_list, next_selected = core.soft_delete_lab_preset(
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
# Setup wizard, per PC (shown when core.setup_needed(): lab_info.json has no lab,
# or this computer has no lab yet)
# ---------------------------------------------------------------------------


# What to do on the very first computer of a lab, when lab_info.json has no labs
# (core.UI_TIPS, shared with the web face).
WIZARD_NO_LABS_TIP = core.ui_tip("wizard_no_labs")


class FirstRunWizard(object):
    """The per-PC setup wizard, three steps (core.WIZARD_STEPS: Lab, Postgres,
    Databases). Copying only lab_info.json to a new PC is enough: step 1 picks
    this PC's lab from it (or creates labs when there are none), step 2 takes
    this computer's Postgres superuser (host localhost), step 3 creates or links
    this PC's databases (the default one prefilled with the lab's suggested
    name) and its button SAVES through core.finish_machine_setup: there is no
    separate review step.

    Next on step 2 tests the login first (core.wizard_postgres_next): a login
    that cannot work keeps the wizard on step 2 with the reason. Skip on step 2
    (no Postgres here) skips the Databases step too and saves at once; Skip on
    step 3 saves without a database. The face only renders; core decides. On
    success self.ok is True."""

    def __init__(self, root, creator=None, pg_tester=None):
        self.root = root
        self.ok = False
        self.creator = creator          # tests inject a fake create_database
        self.pg_tester = pg_tester      # tests inject a fake Postgres login test
        self.step = 0
        self.state = core.setup_state()
        self.new_labs = []              # only when lab_info.json has no labs
        self.home_lab = tk.StringVar(root, value=self.state.get("home_lab") or "")
        admin = self.state.get("default_admin") or {}
        self.admin_user = tk.StringVar(root, value=admin.get("username") or "admin")
        self.admin_pw = tk.StringVar(root, value=admin.get("password") or "")
        pg = self.state.get("pg_admin") or {}
        self.pg_user = tk.StringVar(root, value=pg.get("admin_username") or "postgres")
        self.pg_pw = tk.StringVar(root, value=pg.get("admin_password") or "")
        self.pg_port = tk.StringVar(root, value=pg.get("admin_port") or "5432")
        self.pg_skipped = False
        self.db_skipped = False
        self.db_rows = []               # [{name,user,pw,mode,title: StringVar}]
        self._db_rows_for = None        # the lab the default row was prefilled for
        existing = self.state.get("default_database") or ""
        self.default_choice = tk.StringVar(root, value=("id:" + existing) if existing else "new:0")
        self.pg_result = ""
        self.pg_result_ok = False
        self._more_shown = False        # step 1 "More options" (no labs yet)

        top = tk.Toplevel(root)
        self.top = top
        top.title("%s: set up this computer" % APP_NAME)
        # First run withdraws the main root before showing this wizard. A
        # transient Toplevel whose master is withdrawn never becomes viewable on
        # macOS/aqua, so a later wait_visibility() on it would block forever. Only
        # tie it to the root when the root is actually on screen; otherwise the
        # wizard stands alone and maps itself (see run_first_run_wizard).
        try:
            if root.winfo_viewable():
                top.transient(root)
        except tk.TclError:
            pass
        top.protocol("WM_DELETE_WINDOW", self._cancel)
        top.columnconfigure(0, weight=1)
        tk.Label(top, text="Set up this computer for the lab",
                 font=("TkDefaultFont", 13, "bold"), anchor="w").grid(
            row=0, column=0, sticky="ew", padx=12, pady=(10, 0))
        self.rail = tk.Label(top, text="", fg="#5d6874", anchor="w")
        self.rail.grid(row=1, column=0, sticky="ew", padx=12, pady=(2, 6))
        self.body = tk.Frame(top)
        self.body.grid(row=2, column=0, sticky="nsew", padx=12)
        self.status = tk.Label(top, text="", fg="#b00", justify="left", wraplength=600,
                               anchor="w")
        self.status.grid(row=3, column=0, sticky="ew", padx=12, pady=(8, 0))
        btns = tk.Frame(top)
        btns.grid(row=4, column=0, sticky="ew", padx=12, pady=10)
        tk.Button(btns, text="Cancel", command=self._cancel).pack(side="left")
        self.next_btn = tk.Button(btns, text="Next →", command=self.next_step,
                                  font=("TkDefaultFont", 10, "bold"))
        self.next_btn.pack(side="right")
        self.skip_btn = tk.Button(btns, text="Skip", command=self.skip_step)
        self.skip_btn.pack(side="right", padx=(0, 8))
        self.back_btn = tk.Button(btns, text="← Back", command=self.back_step)
        self.back_btn.pack(side="right", padx=(0, 8))
        self.render()
        _center_over(top, root)

    @property
    def last_step(self):
        return len(core.WIZARD_STEPS) - 1

    # -- rendering ---------------------------------------------------------
    def render(self):
        for child in list(self.body.winfo_children()):
            child.destroy()
        steps = list(core.WIZARD_STEPS)
        self.rail.configure(text="   ›   ".join(
            ("[%d %s]" if i == self.step else "%d %s") % (i + 1, name)
            for i, name in enumerate(steps)))
        self.status.configure(text="", fg="#b00")
        [self._render_lab, self._render_pg, self._render_dbs][self.step]()
        self.back_btn.configure(state=("disabled" if self.step == 0 else "normal"))
        self.skip_btn.pack_forget()
        self.back_btn.pack_forget()
        if self.step in (1, 2):
            # Skipping Postgres skips the Databases step too, so both skips finish.
            self.skip_btn.configure(
                text="Skip (no Postgres)" if self.step == 1 else "Skip")
            self.skip_btn.pack(side="right", padx=(0, 8))
        self.back_btn.pack(side="right", padx=(0, 8))
        self.next_btn.configure(text=("Save" if self.step == self.last_step else "Next →"))

    def _heading(self, text, tip=""):
        """A step heading; its explanation (if any) sits behind an info tip."""
        row = tk.Frame(self.body)
        row.pack(fill="x", pady=(0, 6))
        tk.Label(row, text=text, font=("TkDefaultFont", 11, "bold"),
                 anchor="w").pack(side="left")
        if tip:
            info_tip(row, tip).pack(side="left")
        return row

    def _render_lab(self):
        if self.state["has_labs"]:
            self._heading("Which lab is this computer?")
            if not self.home_lab.get() and self.state["labs"]:
                self.home_lab.set(self.state["labs"][0]["id"])
            for lab in self.state["labs"]:
                tk.Radiobutton(self.body, variable=self.home_lab, value=lab["id"],
                               anchor="w", justify="left",
                               text="%s   (%d seats · %s · room %s)" % (
                                   lab["name"], lab["seats"], lab["host"] or "no host",
                                   lab["default_room"])).pack(fill="x")
            return
        self._heading("Add your lab(s)", WIZARD_NO_LABS_TIP)
        self._build_lab_form(self.body)

    def _build_lab_form(self, parent):
        self.lab_list = tk.Listbox(parent, height=3, width=74, exportselection=False)
        self.lab_list.pack(fill="x")
        for lab in self.new_labs:
            self.lab_list.insert("end", self._lab_line(lab))
        tk.Button(parent, text="Remove selected", command=self._remove_lab).pack(anchor="e")
        # Only what a lab needs: Name, Host, Seats. The rest (default room,
        # shortcut label, map, the default oTree admin login) has a working
        # default and sits under "More options".
        form = tk.Frame(parent)
        form.pack(fill="x", pady=(4, 0))
        tk.Label(form, text="Name").grid(row=0, column=0, sticky="w")
        tk.Label(form, text="Host / IP").grid(row=0, column=1, sticky="w")
        self.w_name = tk.Entry(form, width=22)
        self.w_name.grid(row=1, column=0, padx=(0, 6))
        self.w_host = tk.Entry(form, width=22)
        self.w_host.grid(row=1, column=1, padx=(0, 6))
        tk.Button(form, text="Add lab", command=self._add_lab).grid(row=1, column=2)
        tk.Label(form, text="Seats (one per line, or space/comma separated)").grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.w_seats = tk.Text(form, width=68, height=3)
        self.w_seats.grid(row=3, column=0, columnspan=3, sticky="ew")

        self.more_link = tk.Label(parent, text="", fg="#5d6874", anchor="w", cursor="hand2")
        self.more_link.pack(anchor="w", pady=(8, 0))
        self.more_link.bind("<Button-1>", lambda _e: self._toggle_more())
        more = self.more_frame = tk.Frame(parent)
        room_head = tk.Frame(more)
        room_head.grid(row=0, column=0, sticky="w")
        tk.Label(room_head, text="Default oTree room").pack(side="left")
        info_tip(room_head, core.ROOM_HELP).pack(side="left")
        short_head = tk.Frame(more)
        short_head.grid(row=0, column=1, sticky="w", padx=(0, 10))
        tk.Label(short_head, text="Shortcut label (optional)").pack(side="left")
        info_tip(short_head, core.ui_tip("shortcut_label")).pack(side="left")
        tk.Label(more, text="Map").grid(row=0, column=2, sticky="w")
        self.w_room = tk.Entry(more, width=18)
        self.w_room.insert(0, DEFAULT_ROOM_NAME)
        self.w_room.grid(row=1, column=0, padx=(0, 6), sticky="w")
        self.w_shortcut = tk.Entry(more, width=18)
        self.w_shortcut.grid(row=1, column=1, padx=(0, 6), sticky="w")
        self.w_map = ttk.Combobox(more, width=16, state="readonly",
                                  values=["(plain grid)"] + list(self.state.get("maps") or []))
        self.w_map.set("(plain grid)")
        self.w_map.grid(row=1, column=2, padx=(0, 6), sticky="w")
        af = tk.Frame(more)
        af.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        tk.Label(af, text="Default oTree admin login for new configs",
                 font=("TkDefaultFont", 10, "bold")).grid(row=0, column=0, columnspan=4,
                                                          sticky="w")
        tk.Label(af, text="Username").grid(row=1, column=0, sticky="w")
        tk.Entry(af, textvariable=self.admin_user, width=16).grid(row=1, column=1, padx=(4, 12))
        tk.Label(af, text="Password").grid(row=1, column=2, sticky="w")
        tk.Entry(af, textvariable=self.admin_pw, width=16, show="*").grid(row=1, column=3, padx=4)
        self.example_btn = tk.Button(parent, text="Use example values (localhost)",
                                     command=self._use_example)
        self.example_btn.pack(anchor="w", pady=(8, 0))
        self._apply_more()

    def _apply_more(self):
        self.more_link.configure(
            text=("▾  More options" if self._more_shown else "▸  More options"))
        if self._more_shown:
            self.more_frame.pack(fill="x", pady=(4, 0), before=self.example_btn)
        else:
            self.more_frame.pack_forget()

    def _toggle_more(self):
        """Show/hide the optional lab fields WITHOUT rebuilding the form (what
        was typed stays)."""
        self._more_shown = not self._more_shown
        self._apply_more()

    @staticmethod
    def _lab_line(lab):
        return "%s · %s · %d seats · room: %s%s" % (
            lab["name"], lab["host"], len(lab["seats"]), lab["default_room"],
            "" if not lab.get("map") else " · map: " + lab["map"])

    def _lab_form_filled(self):
        """True when something is typed in the lab form that was not added yet."""
        try:
            return bool(self.w_name.get().strip() or self.w_host.get().strip()
                        or self.w_seats.get("1.0", "end").strip())
        except (tk.TclError, AttributeError):
            return False

    def _add_lab(self):
        name = self.w_name.get().strip()
        host = self.w_host.get().strip()
        seats = core.parse_seat_list(self.w_seats.get("1.0", "end"))
        ok, message, seats = core.validate_lab_preset_fields(name, host, seats)
        if not ok:
            self.status.configure(text=message)
            return False
        map_choice = self.w_map.get()
        lab = {"name": name, "host": host, "seats": seats,
               "map": "" if map_choice == "(plain grid)" else map_choice,
               "default_room": core.sanitize_room_name(self.w_room.get()),
               "shortcut_label": self.w_shortcut.get().strip()}
        self.new_labs.append(lab)
        self.lab_list.insert("end", self._lab_line(lab))
        for entry in (self.w_name, self.w_host, self.w_shortcut):
            entry.delete(0, "end")
        self.w_seats.delete("1.0", "end")
        self.w_room.delete(0, "end")
        self.w_room.insert(0, DEFAULT_ROOM_NAME)
        self.status.configure(text="")
        return True

    def _remove_lab(self):
        for index in reversed(list(self.lab_list.curselection())):
            self.lab_list.delete(index)
            del self.new_labs[index]

    def _use_example(self):
        example = core.example_lab_info_to_save()
        if not example:
            self.status.configure(text="Could not read the example lab_info.json.")
            return
        try:
            core.save_lab_info(example)
            core.reload_lab_info()
        except Exception as error:   # noqa: BLE001 - shown to the user
            self.status.configure(text="Could not save: %s" % error)
            return
        self.state = core.setup_state()
        self.new_labs = []
        self.render()

    def _new_lab_home(self):
        """The id this PC gets when it creates the labs: the selected row, else
        the first lab."""
        index = getattr(self, "_new_home_index", 0)
        if not 0 <= index < len(self.new_labs):
            index = 0
        return core.new_lab_id(self.new_labs[index]["name"], [])

    def _render_pg(self):
        self._heading("Postgres on this computer", core.ui_tip("wizard_postgres"))
        form = tk.Frame(self.body)
        form.pack(fill="x")
        pw_holder = tk.Frame(form)
        tk.Entry(pw_holder, textvariable=self.pg_pw, width=22, show="*").pack(side="left")
        # The superuser's password may be blank (Postgres.app, a trust login):
        # behind the tip, not a grey line.
        info_tip(pw_holder, core.ui_tip("pg_blank_password")).pack(side="left")
        rows = [("Superuser", tk.Entry(form, textvariable=self.pg_user, width=22)),
                ("Password", pw_holder),
                ("Port", tk.Entry(form, textvariable=self.pg_port, width=8))]
        host = tk.Entry(form, width=22)
        host.insert(0, "localhost")
        host.configure(state="readonly")
        rows.insert(2, ("Host", host))
        for r, (label, widget) in enumerate(rows):
            tk.Label(form, text=label).grid(row=r, column=0, sticky="w", pady=2, padx=(0, 8))
            widget.grid(row=r, column=1, sticky="w", pady=2)
        tk.Button(self.body, text="Test connection", command=self.test_connection).pack(
            anchor="w", pady=(8, 0))
        self.pg_result_label = tk.Label(
            self.body, text=self.pg_result, anchor="w", justify="left", wraplength=600,
            fg=("#1a7f4b" if self.pg_result_ok else "#b3261e"))
        self.pg_result_label.pack(fill="x")
        # One instruction, with what skipping means behind the tip.
        skip = tk.Frame(self.body)
        skip.pack(fill="x", pady=(10, 0))
        tk.Label(skip, text="No Postgres on this computer? Press Skip.", fg="#5d6874",
                 anchor="w").pack(side="left")
        info_tip(skip, core.WIZARD_NO_PG_NOTE).pack(side="left")

    def _typed_pg_admin(self):
        return {"admin_username": self.pg_user.get(), "admin_password": self.pg_pw.get(),
                "admin_port": self.pg_port.get()}

    def pg_admin(self):
        """The Postgres login typed in step 2, or None when skipped/incomplete.
        The password may be blank (a superuser with no password)."""
        if self.pg_skipped or not self.pg_user.get().strip():
            return None
        return core.wizard_pg_admin(self._typed_pg_admin())

    def _show_pg_result(self, ok, message):
        self.pg_result, self.pg_result_ok = message, bool(ok)
        try:
            self.pg_result_label.configure(
                text=message, fg=("#1a7f4b" if ok else "#b3261e"))
        except (tk.TclError, AttributeError):
            pass

    def test_connection(self):
        tester = self.pg_tester or core.test_pg_admin_connection
        result = tester(self._typed_pg_admin())
        self._show_pg_result(result.get("ok"), result.get("message", ""))
        return result

    def _chosen_lab(self):
        if self.state["has_labs"]:
            return self.home_lab.get()
        return self._new_lab_home() if self.new_labs else ""

    def _ensure_default_row(self):
        lab = self._chosen_lab()
        if self._db_rows_for == lab and self.db_rows:
            return
        suggested = core.suggested_database_name(lab)
        if self.db_rows and self._db_rows_for is not None:
            row = self.db_rows[0]
            row["name"].set(suggested.get("db_name", ""))
            row["user"].set(suggested.get("db_user", ""))
        else:
            self.db_rows = [self._new_row(suggested.get("db_name", ""),
                                          suggested.get("db_user", ""))]
        self._db_rows_for = lab

    def _new_row(self, name="", user=""):
        # "adv" off (the default) = no user of its own: owned by, and connecting
        # as, the Postgres admin login. On = a dedicated user (prefilled with the
        # lab's suggested user).
        return {"name": tk.StringVar(self.root, value=name),
                "user": tk.StringVar(self.root, value=user),
                "pw": tk.StringVar(self.root, value=""),
                "adv": tk.BooleanVar(self.root, value=False),
                "mode": tk.StringVar(self.root, value=("create" if self.pg_admin() else "link"))}

    def create_allowed(self):
        """Create needs a Postgres login (step 2 not skipped)."""
        return self.pg_admin() is not None

    def _render_dbs(self):
        self._ensure_default_row()
        self._heading("Databases on this computer", core.ui_tip("wizard_databases"))
        can_create = self.create_allowed()
        if not can_create:
            for row in self.db_rows:
                row["mode"].set("link")
            tk.Label(self.body, text=core.WIZARD_NO_PG_NOTE, fg="#8a5a00", anchor="w",
                     justify="left", wraplength=600).pack(fill="x", pady=(0, 6))
        grid = tk.Frame(self.body)
        grid.pack(fill="x")
        # Without a Postgres login there is no admin login to use: own user.
        own_rows = [bool(row["adv"].get()) or not can_create for row in self.db_rows]
        any_own = any(own_rows)
        tk.Label(grid, text="Default", fg="#5d6874").grid(row=0, column=0, sticky="w",
                                                           padx=(0, 8))
        tk.Label(grid, text="Database", fg="#5d6874").grid(row=0, column=1, sticky="w")
        own_head = tk.Frame(grid)
        own_head.grid(row=0, column=2, sticky="w", padx=(8, 8))
        tk.Label(own_head, text="Own user", fg="#5d6874").pack(side="left")
        # Un-ticked = the Postgres admin login; ticked = a dedicated user.
        info_tip(own_head, "Not ticked: %s Ticked: a dedicated database user."
                 % core.ADMIN_LOGIN_NOTE).pack(side="left")
        if any_own:
            # The User / Password columns exist only while a row has its own user.
            tk.Label(grid, text="User", fg="#5d6874").grid(row=0, column=3, sticky="w")
            pw_head = tk.Frame(grid)
            pw_head.grid(row=0, column=4, sticky="w")
            tk.Label(pw_head, text="Password", fg="#5d6874").pack(side="left")
            info_tip(pw_head, core.ui_tip("pg_new_user_password")).pack(side="left")
        self.create_buttons = []
        self.user_entries = []
        for r, row in enumerate(self.db_rows, start=1):
            tk.Radiobutton(grid, variable=self.default_choice,
                           value="new:%d" % (r - 1)).grid(row=r, column=0)
            tk.Entry(grid, textvariable=row["name"], width=18).grid(row=r, column=1, padx=2)
            own = own_rows[r - 1]
            tk.Checkbutton(grid, variable=row["adv"], command=self.render,
                           state=("normal" if can_create else "disabled")).grid(
                row=r, column=2)
            if own:
                user_entry = tk.Entry(grid, textvariable=row["user"], width=12)
                user_entry.grid(row=r, column=3, padx=2)
                tk.Entry(grid, textvariable=row["pw"], width=12, show="*").grid(
                    row=r, column=4, padx=2)
                self.user_entries.append(user_entry)
            modes = tk.Frame(grid)
            modes.grid(row=r, column=5, padx=4)
            create = tk.Radiobutton(modes, text="Create", variable=row["mode"], value="create",
                                    state=("normal" if can_create else "disabled"))
            create.pack(side="left")
            self.create_buttons.append(create)
            tk.Radiobutton(modes, text="Link existing", variable=row["mode"],
                           value="link").pack(side="left")
            if r > 1:
                tk.Button(grid, text="×", command=lambda i=r - 1: self._remove_row(i)).grid(
                    row=r, column=6)
        tk.Button(self.body, text="+ Add another database", command=self._add_row).pack(
            anchor="w", pady=(4, 0))
        existing = self.state.get("databases") or []
        if existing:
            tk.Label(self.body, text="Already on this computer",
                     font=("TkDefaultFont", 10, "bold"), anchor="w").pack(fill="x", pady=(10, 0))
            for entry in existing:
                line = tk.Frame(self.body)
                line.pack(fill="x")
                tk.Radiobutton(line, text=entry["title"], variable=self.default_choice,
                               value="id:" + entry["id"], anchor="w").pack(side="left")
                if entry.get("host_note"):
                    tk.Label(line, text="  " + entry["host_note"], fg="#8a94a1").pack(
                        side="left")
                if entry.get("location_warning"):
                    # The short note; the full sentence is behind the tip.
                    short = core.database_location_note(entry) or "Created on another computer"
                    tk.Label(line, text="  " + short, fg="#8a5a00").pack(side="left")
                    info_tip(line, entry["location_warning"]).pack(side="left")

    def _add_row(self):
        self.db_rows.append(self._new_row())
        self.render()

    def _remove_row(self, index):
        del self.db_rows[index]
        if self.default_choice.get() == "new:%d" % index:
            self.default_choice.set("new:0")
        self.render()

    def payload(self):
        """The core.finish_machine_setup keyword arguments this wizard collected."""
        databases, index_map = [], {}
        if not self.db_skipped:
            for i, row in enumerate(self.db_rows):
                name = row["name"].get().strip()
                if not name:
                    continue
                index_map[i] = len(databases)
                # Own user off (and a Postgres login present) = the admin login.
                own = bool(row["adv"].get()) or not self.create_allowed()
                databases.append({"db_name": name,
                                  "db_user": row["user"].get().strip() if own else "",
                                  "db_password": row["pw"].get() if own else "",
                                  "title": name,
                                  "create": row["mode"].get() == "create"
                                  and self.create_allowed()})
        choice = self.default_choice.get()
        default_index, default_id = 0, None
        if choice.startswith("id:"):
            default_id = choice[3:]
        elif choice.startswith("new:"):
            try:
                default_index = index_map.get(int(choice[4:]), 0)
            except ValueError:
                default_index = 0
        kwargs = {"home_lab": self._chosen_lab(), "pg_admin": self.pg_admin(),
                  "databases": databases, "default_index": default_index,
                  "default_database_id": default_id}
        if not self.state["has_labs"]:
            kwargs["new_labs"] = list(self.new_labs)
            kwargs["default_admin"] = {"username": self.admin_user.get().strip() or "admin",
                                       "password": self.admin_pw.get()}
        return kwargs

    # -- navigation --------------------------------------------------------
    def next_step(self):
        if self.step == 0:
            if self.state["has_labs"] and not self.home_lab.get():
                self.status.configure(text="Choose which lab this computer is.")
                return
            if not self.state["has_labs"]:
                # A lab that is typed but not added yet is added by Next (one
                # click less; an incomplete one says what is missing).
                if self._lab_form_filled() and not self._add_lab():
                    return
                ok, message = core.validate_wizard_labs(self.new_labs)
                if not ok:
                    self.status.configure(text=message)
                    return
                try:
                    sel = list(self.lab_list.curselection())
                except (tk.TclError, AttributeError):
                    sel = []
                self._new_home_index = sel[0] if sel else 0
        elif self.step == 1:
            # Test the login NOW (core decides): a login that cannot work stays
            # here with the reason instead of failing at Save; no superuser typed
            # is the same as Skip.
            self.pg_skipped = False
            outcome = core.wizard_postgres_next(self.pg_admin(), tester=self.pg_tester)
            if outcome["action"] == "skip":
                self.skip_step()
                return
            if outcome["action"] == "stay":
                self._show_pg_result(False, outcome["message"])
                self.status.configure(
                    text="Fix the Postgres login, or press Skip (no Postgres).")
                return
            self._show_pg_result(True, outcome.get("message", ""))
        if self.step == self.last_step:
            self.db_skipped = False
            self.save()
            return
        self.step += 1
        self.render()

    def skip_step(self):
        """Skip on Postgres = no Postgres on this computer: a database cannot be
        created without it, so the Databases step is skipped too and the wizard
        saves. Skip on Databases saves without a database."""
        if self.step == 1:
            self.pg_skipped = True
            self.db_skipped = True
        elif self.step == 2:
            self.db_skipped = True
        else:
            return
        self.save()

    def back_step(self):
        if self.step > 0:
            self.step -= 1
            if self.step == 1:
                self.pg_skipped = False
            self.db_skipped = False
            self.render()

    def save(self):
        kwargs = self.payload()
        if self.creator is not None:
            kwargs["creator"] = self.creator
        try:
            result = core.finish_machine_setup(**kwargs)
        except Exception as error:   # noqa: BLE001 - shown to the user
            result = {"ok": False, "message": "%s: %s" % (type(error).__name__, error)}
        self.result = result
        if not result.get("ok"):
            self.status.configure(text=result.get("message") or "Could not save.", fg="#b3261e")
            if self.step == self.last_step:
                self.next_btn.configure(text="Retry")
            return result
        self.ok = True
        _modal_close(self.top)
        warnings = result.get("warnings") or []
        if warnings:
            # A database kept although its user cannot log in: say so clearly.
            try:
                messagebox.showwarning("Check these databases", "\n\n".join(warnings),
                                       parent=self.root)
            except tk.TclError:
                pass
        return result

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
    """Show the per-PC setup wizard modally. Returns True when this computer
    was set up, False if the user cancelled."""
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





def main():
    # Upgrade an old data folder (and load the live state) BEFORE any window.
    storage = core.prepare_storage()
    refresh_defaults_from_core()
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", root.tk.call("tk", "scaling"))
    except tk.TclError:
        pass
    if storage.get("newer_schema"):
        root.withdraw()
        messagebox.showerror(APP_NAME, storage.get("message") or
                             "This data folder was saved by a newer launcher.")
        root.destroy()
        return
    # This computer is not set up yet (no labs, or no lab chosen for this PC):
    # run the setup wizard before the main window is built.
    if core.setup_needed():
        root.withdraw()
        if not run_first_run_wizard(root):
            root.destroy()
            return
        core.reload_lab_info()
        refresh_defaults_from_core()
        root.deiconify()
    app = LauncherApp(root)
    if not storage.get("ok", True) and storage.get("message"):
        app.log(storage["message"], "warn")
        app.show_notices([storage["message"]], warning=True)
    app.show_notices(core.take_notices())
    # The stored GitHub token is about to expire (or has): say so, amber.
    app.show_notices([core.github_token_expiry_notice()], warning=True)
    root.mainloop()


if __name__ == "__main__":
    # The headless --run path already dispatched and exited ABOVE, before the
    # tkinter import, so reaching here is always a normal GUI launch.
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - last-resort startup diagnostics
        _log_startup_crash(exc)
        raise
