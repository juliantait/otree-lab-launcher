# oTree Lab Launcher

**Make any working oTree project run in your lab with one click.** Point it at your project and it wires up Postgres, per-seat participant links, and the experiment room, then starts the server ready for participants.

## What it does

- **Select your database once.** Postgres, wired for you: the launcher builds the `DATABASE_URL` and hands it to oTree.
- **Consistent per-seat links.** Each lab PC opens the same link every session, so you always know who's who.
- **One button to launch.** Save experimental configs to relaunch them quickly in follow-up sessions.
- **Lab defaults.** Once the launcher is set up, any oTree project is lab-ready in one click.

## Which launcher to run

Two versions, same app:

- **Simple** - Tkinter launcher.
- **Web** - a prettier UI over the same app.

Runs on macOS (the `Mac_...command` files) and Windows (the `Win_...vbs` files). Both run the app code in `app/`; use whichever you prefer.

## Quick start

First launch opens the **setup wizard** (it appears whenever there is no `data/lab_info.json`). Two roles:

**Lab manager - set up once:**

1. Set up Postgres.
2. Create the default database everyone shares.
3. Create a shared room and prep the participant PCs with that room's link.

**Any researcher - three clicks:**

1. Point the oTree launcher at your project.
2. Run or select the lab defaults.
3. Hit **Launch session**.

## Technical

**Folder layout**

```
<repo root>/
  Mac_Start oTree Lab Launcher.command            (Simple, macOS)
  Win_Start oTree Lab Launcher.vbs                (Simple, Windows)
  Mac_Start oTree Lab Launcher (web).command      (Web, macOS)
  Win_Start oTree Lab Launcher (web).vbs          (Web, Windows)
  app/    the app code
  data/   your config and maps (lab_info.json, presets.json, maps/, ...)
```

**Updating.** Copy the new version over the top and keep your `data/` folder. Labs, saved configs, the database registry and this machine's identity all carry over.

**Requirements.** Python 3 (the Simple app uses the standard-library `tkinter`). Your oTree project, runnable with `otree`. To use the create-a-database button: PostgreSQL reachable from the machine and `psycopg2` (`pip install psycopg2-binary`); an existing database needs only its connection details.

---

## Appendix: lab manager guide

The detailed setup guide. This is a one-time job for whoever looks after the lab machines. Individual researchers do not do any of it: they open the launcher, point it at their oTree project, pick the room, and hit **Launch session**.

### One-time setup in the wizard

1. Install the launcher: copy the whole folder onto the machine that will run sessions.
2. Run it. On the first launch, with no `data/lab_info.json` yet, the **setup wizard** opens on its own.
3. In the wizard, set the **Postgres admin password**.
4. Create the default shared **"easy-use" database** that every researcher can then use in one click.
5. Add your lab(s): name, host, and seat list.

From then on, researchers pick the shared default database and launch. They can also create their own database at any time from the in-app buttons, so you do not have to make one per study.

### lab_info.json

Your hosts, seat lists, and room-map references live in `data/lab_info.json` (see `data/lab_info.example.json` for the shape). Hosts and passwords are also editable from inside the app (gear -> Lab Settings), so you rarely need to hand-edit the file.

Room layouts (the top-down seat maps the launcher draws) live in `data/maps/`. Each lab points at a map by name. The schema and how to add your own room are in `data/maps/README.md`.

### The per-seat links (the important part)

Each lab PC opens its own fixed link:

```
http://HOST:8000/room/study?participant_label=SEAT
```

- `HOST` is the machine that launches the session (the one running the launcher).
- `study` is the room.
- `SEAT` is that computer's seat label.

Each link is always active. Put the correct link on each desktop once: set it as the browser homepage, or save it as a bookmark, one seat per machine. Do this once per lab and you never touch it again.

When a participant opens their machine's link, that seat flips from grey to green on the experimenter's monitor. That is how the consistent links track who's who in practice: one machine, one seat, always the same person's spot.

### Room warning

The desktop shortcuts point at the `study` room. If a study uses a different room, the links must point at that room instead:

```
http://HOST:8000/room/THEIRROOM?participant_label=SEAT
```

The launch confirmation popup warns you about this when the chosen room is not `study`, and shows the correct link to use.

### Saving configs

You can save a config per study. A saved config relaunches the study in one click in later sessions, so the common studies are always a double-click away.
