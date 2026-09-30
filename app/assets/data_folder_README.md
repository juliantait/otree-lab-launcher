# The data/ folder: your settings live here

`data/` (next to `app/`) holds everything the launcher reads and writes. It is
**yours**: nothing in it ships with the app, git ignores the whole folder, so an
update (`git pull`) never touches it. The files the app ships (this text, the
example lab settings, the example room maps) live in `app/assets/`.

## Three settings files, three scopes

| File | Scope | Holds |
|------|-------|-------|
| `lab_info.json` | **the lab** (copy it to every computer of the lab) | the labs (name, host, seats, map, default room, participant-shortcut label, suggested database name), the room maps, the default oTree admin login for new configs, the GitHub Organisation Sync switch + organisation, "Databases on this computer only" |
| `machine.json` | **this computer only** (never copy it) | which lab this computer is, which labs it shows, the light/dark theme, the Postgres superuser login, this computer's databases (with their passwords; a database that uses the Postgres superuser login stores only `uses_admin_login: true`, never a copy of that login) and its default database |
| `saved_configs.json` | **this computer only** | your saved launch configs (each points at one of this computer's databases by id and keeps its own oTree admin login) and the researcher list |

## Written by the launcher

- `launch_history.jsonl` - one line per launch (the Launch history viewer).
- `seats/` - generated participant-label files.
- `locks/` - short-lived lock files (removed after use).
- `update_check.json`, `otree-lab-launcher.log`, `web_launcher.log`.
- `retired/` - old-format files moved aside by a settings upgrade; each
  folder has a README.txt. Safe to delete once the launcher works.

## Setting up another computer of the lab

Copy only `lab_info.json` into that computer's `data/` folder and start the
launcher. The setup wizard asks which lab the computer is, the Postgres
superuser login of that computer (or skip it: launches then use oTree's own
SQLite), and creates that computer's databases, pre-filled with the lab's
suggested database name.

Test studies and GitHub clones belong in `local/` at the repo root, not here.

The environment variables `OTREE_LAB_DATA_DIR`, `OTREE_LAB_INFO`,
`OTREE_LAB_MACHINE` and `OTREE_LAB_LAUNCHER_PRESETS` override these locations.
