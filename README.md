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
