# oTree Lab Launcher

**Make any working oTree project run in your lab with one click.** Point it at
your project and it wires up Postgres, participant label lists, and the
experiment room, then starts the server ready for participants to join.

Each lab PC opens its own per-seat link
(`http://HOST:8000/room/study?participant_label=SEAT`) — you place the correct
link on each desktop once; arrivals then show up on the monitor.

---

## What it does

- **Postgres, wired for you.** Enter your database once (or let the launcher
  create a fresh database and user for you); it builds the `DATABASE_URL` and
  hands it to oTree.
- **Seats and rooms, ready to go.** It generates the participant label file and
  points the experiment at the lab room, so every seat has a stable link.
- **One button to launch.** A confirmation screen names the lab, the host, and
  the exact per-seat link before the server starts. Nothing runs until you say
  so.
- **It never edits your experiment code** — it only writes its own files. (The
  one exception is an explicit, clearly-marked, fully-revertible "Add block to
  settings.py" button you choose to click.)

## Which launcher to run

The repo ships four start scripts. Pick by your operating system and which
version of the app you want.

- **File extension tells you the OS.** `.bat` files are for **Windows**
  (double-click to run). `.command` files are for **macOS** (double-click to
  run).
- **Two versions, same app.** Both versions do exactly the same thing, with the
  same features and the same logic. They only look different.
  - **`Start oTree Lab Launcher`** is the **main app**: the standard Tkinter
    desktop launcher. This is the primary, most reliable one, so use it if you
    are not sure which to pick.
  - **`Start oTree Lab Launcher (web)`** is a **prettier version of the same
    app**: identical features and behaviour, just a nicer-looking UI.

So the four scripts are:

- `Start oTree Lab Launcher.bat` — main app, Windows
- `Start oTree Lab Launcher.command` — main app, macOS
- `Start oTree Lab Launcher (web).bat` — web version, Windows
- `Start oTree Lab Launcher (web).command` — web version, macOS

A lab can use whichever it prefers.

## Quick start

1. **Copy the template:** `lab_info.example.json` → `lab_info.json`.
2. **Edit `lab_info.json`:** set each lab's real `host`, and set the database
   and admin passwords. (`lab_info.json` is git-ignored — it holds your real
   hosts and passwords and must never be committed.)
3. **Run the launcher (Windows):** double-click
   **`Start oTree Lab Launcher.bat`** (or run it from a command prompt).

   The first run with **no** `lab_info.json` opens a **setup wizard** that walks
   you through creating one — so you can also just launch it and follow along.

Then point the launcher at your oTree project folder, pick the lab and room, and
hit **Launch session**.

## Two roles: set it up once, then just launch

The setup only has to happen once. Whoever runs the lab machines (a lab manager,
or anyone looking after the infrastructure) installs the launcher, fills in
`lab_info.json` with the real hosts, seats, and room maps, and saves the configs
for the studies that will run. That is a one-time job.

After that, individual experimenters do not touch any of that setup. They just
use what is already in place: open the launcher, point it at their own oTree
project (or double-click a saved one-click shortcut), and launch. The database,
seats, rooms, and per-seat links are all handled for them by the setup the lab
manager put in place, so getting a study running in the lab is quick and needs
no technical fiddling.

## Labs, seats, and room maps

- Your labs, their hosts, seat lists, database, and admin login all live in
  **`lab_info.json`** (hand-edited JSON — see `lab_info.example.json` for the
  shape). Hosts and passwords are also editable from inside the app.
- Room layouts (the top-down seat maps the launcher draws) live in **`maps/`**.
  Each lab points at a map by name. To add your own room, drop a
  `maps/<name>.json` and reference it — full instructions and the schema are in
  [`maps/README.md`](maps/README.md).

**Rule of thumb:** seat lists and room maps are hand-edited JSON; hosts and
credentials are entered in the app.

## What each lab PC opens

Every seat opens its own link:

```
http://HOST:8000/room/study?participant_label=SEAT
```

`HOST` is the launch machine, `study` is the room, and `SEAT` is that
computer's seat. You set each desktop's link once; when a participant opens it,
that seat turns green on the experimenter's monitor.

## Requirements

- Python 3 (the standard library `tkinter` GUI ships with it).
- Your oTree project, runnable with `otree` on the launch machine.
- To use the "create a database" button: PostgreSQL reachable from the launch
  machine, and `psycopg2` (`pip install psycopg2-binary`). A database you have
  already created needs no extra libraries — just the connection details.
