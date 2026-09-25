# oTree Lab Launcher

**Make any working oTree project run in your lab with one click.** Point it at your project and it wires up Postgres, per-seat participant links, and the experiment room, then starts the server ready for participants.

See [docs/user_guide.md](docs/user_guide.md) for the full user guide (lab manager setup and experimenter usage).

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

## Alternative launchers

For normal use, ignore this section: the default browser launcher at the folder root (`Win_Start oTree Lab Launcher.vbs` / `Mac_Start oTree Lab Launcher.command`) is the one everyone should use. The variants below live in `alternative_launchers/` and exist only for troubleshooting or personal preference. All of them run the same app code in `app/`.

- **`(simple)`** - `Mac_Start oTree Lab Launcher (simple).command` / `Win_Start oTree Lab Launcher (simple).vbs`. The standard-library **Tkinter** launcher (a native window instead of the browser). Handy if you would rather not use a browser tab.
- **`debug_`** - `debug_Win_Start oTree Lab Launcher.bat` (Windows). The default browser launcher, but run in a **visible console** so you can watch the server log and see any crash on screen. The window stays open on exit so errors are readable.
- **`debug_ (simple)`** - `debug_Win_Start oTree Lab Launcher (simple).bat` (Windows). The same, for the simple/Tk launcher.
