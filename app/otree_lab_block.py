# === oTree lab support (paste at the END of settings.py) ===
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
