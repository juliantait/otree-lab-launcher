# oTree Lab Launcher

**Make any working oTree project run in your lab with one click.** Point it at your project and it wires up Postgres, per-seat participant links, and the experiment room, then starts the server ready for participants.

## Install

**Clone the repo (recommended on both macOS and Windows):**

```
git clone https://github.com/juliantait/otree-lab-launcher.git
cd otree-lab-launcher
```

Then double-click the launcher in the folder: `Mac_Start oTree Lab Launcher.command` (macOS) or `Win_Start oTree Lab Launcher.vbs` (Windows).

**Please clone rather than downloading the ZIP.** A downloaded ZIP is marked as quarantined by the operating system, and that causes launch problems: on macOS Gatekeeper prompts ("Apple could not verify...") on every launch, and on Windows it can trip Microsoft Defender SmartScreen. A `git clone` is not quarantined, so it launches cleanly on both. (No git yet? Install it from [git-scm.com](https://git-scm.com/downloads), or on macOS run `xcode-select --install`.)

Once cloned, updating is super easy: just run `git pull` in the folder to get the latest version. See [Updating](#updating) below.

## What it does

- **Select your database once.** Postgres, wired for you: the launcher builds the `DATABASE_URL` and hands it to oTree.
- **Consistent per-seat links.** Each lab PC opens the same link every session, so you always know who's who.
- **One button to launch.** Save experimental configs to relaunch them quickly in follow-up sessions.
- **Lab defaults.** Once the launcher is set up, any oTree project is lab-ready in one click.

## Which launcher to run

**Double-click the launcher in the folder root** - `Mac_Start oTree Lab Launcher.command` (macOS) or `Win_Start oTree Lab Launcher.vbs` (Windows). That is the one everyone should use: it opens the app in your default browser, with nothing to install.

Prefer a desktop icon? Run `Mac_Create desktop shortcut.command` / `Win_Create desktop shortcut.bat` once to drop a branded shortcut on your Desktop that points at it.

(There are a few other variants in `alternative_launchers/` for troubleshooting or preference - see [Alternative launchers](#alternative-launchers) at the bottom. Most people never need them.)

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
  Mac_Start oTree Lab Launcher.command                  (default/browser, macOS)
  Win_Start oTree Lab Launcher.vbs                       (default/browser, Windows, windowless)
  Mac_Create desktop shortcut.command                   (make a Desktop shortcut, macOS)
  Win_Create desktop shortcut.bat                        (make a Desktop shortcut, Windows)
  alternative_launchers/
    Mac_Start oTree Lab Launcher (simple).command       (Simple/Tk, macOS)
    Win_Start oTree Lab Launcher (simple).vbs           (Simple/Tk, Windows, windowless)
    debug_Win_Start oTree Lab Launcher (simple).bat     (Simple/Tk, Windows, visible console)
    debug_Win_Start oTree Lab Launcher.bat              (default/browser, Windows, visible console)
  app/    the app code (and app/branding/ logo files)
  data/   your config and maps (lab_info.json, presets.json, maps/, ...)
```

**Requirements.** Python 3 (the Simple app uses the standard-library `tkinter`). Your oTree project, runnable with `otree`. To use the create-a-database button: PostgreSQL reachable from the machine and `psycopg2` (`pip install psycopg2-binary`); an existing database needs only its connection details.

## Updating

If you cloned the repo, run `git pull` in the folder:

```
git pull
```

That fetches the latest version and leaves your own `data/` config untouched: your `lab_info.json`, `presets.json`, saved shortcuts, the database registry and this machine's identity all carry over.

If you did not clone, you can instead download the repo and drop the `app/` folder over your existing one. But cloning once and using `git pull` is the recommended and easiest way to stay up to date.

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
http://HOST:8000/room/study?participant_label=SEAT&welcome_page_ok=1
```

- `HOST` is the machine that launches the session (the one running the launcher).
- `study` is the room.
- `SEAT` is that computer's seat label.
- `welcome_page_ok=1` skips oTree 6's Welcome/Start page, so the seat auto-admits with no click.

**This exact URL (with `welcome_page_ok=1`) is the link to put in all lab documentation and on the lab computers.** Without the flag, oTree 6 first shows a Welcome/Start page that needs a click; with it, opening the link drops the participant straight into the experiment.

Each link is always active. Put the correct link on each desktop once: set it as the browser homepage, or save it as a bookmark, one seat per machine. Do this once per lab and you never touch it again.

One caveat: the room session must be created first. Before it exists the link shows a wait page, which advances on its own the moment the session opens — no need to re-click or refresh.

When a participant opens their machine's link, that seat flips from grey to green on the experimenter's monitor. That is how the consistent links track who's who in practice: one machine, one seat, always the same person's spot.

### Room warning

The desktop shortcuts point at the `study` room. If a study uses a different room, the links must point at that room instead:

```
http://HOST:8000/room/THEIRROOM?participant_label=SEAT&welcome_page_ok=1
```

The launch confirmation popup warns you about this when the chosen room is not `study`, and shows the correct link to use.

### Saving configs

You can save a config per study. A saved config relaunches the study in one click in later sessions, so the common studies are always a double-click away.

### GitHub Organisation Sync (opt-in, off by default)

An optional convenience that lets a researcher clone and update an experiment repo straight from your lab's GitHub organisation without opening a terminal. It is **off by default** and lives in **gear -> Lab Settings -> GitHub Organisation Sync**: a single tick box shows or hides two buttons, and a field sets the organisation name (so the clone target is `<org>/<repo>` and is not hardcoded). Both the tick box and the organisation name are saved on this computer.

When it is on:

- **GitHub Org.** (next to Browse) asks for a repository name and a destination folder, then clones `https://github.com/<org>/<repo>` in the background and auto-selects the cloned folder as the study folder.
- **Git update** (per config) runs `git pull` in the selected study folder (never the launcher's own folder) and reports one of three outcomes: this folder is not a git repo, Updated, or No updates available on git. A real pull error (auth, network, merge conflict) is shown as text.

**Prerequisite (lab manager job):** this only works if you have already set up git on the lab experimenter PC and signed that PC in read-only to your GitHub organisation (a read-only organisation credential stored once per PC). The launcher never stores or handles any token itself; it relies entirely on the machine credential already on the PC. If that is not set up, leave the feature off.

---

## Alternative launchers

For normal use, ignore this section: the default browser launcher at the folder root (`Win_Start oTree Lab Launcher.vbs` / `Mac_Start oTree Lab Launcher.command`) is the one everyone should use. The variants below live in `alternative_launchers/` and exist only for troubleshooting or personal preference. All of them run the same app code in `app/`.

- **`(simple)`** - `Mac_Start oTree Lab Launcher (simple).command` / `Win_Start oTree Lab Launcher (simple).vbs`. The standard-library **Tkinter** launcher (a native window instead of the browser). Handy if you would rather not use a browser tab.
- **`debug_`** - `debug_Win_Start oTree Lab Launcher.bat` (Windows). The default browser launcher, but run in a **visible console** so you can watch the server log and see any crash on screen. The window stays open on exit so errors are readable.
- **`debug_ (simple)`** - `debug_Win_Start oTree Lab Launcher (simple).bat` (Windows). The same, for the simple/Tk launcher.
