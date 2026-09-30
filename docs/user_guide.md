# oTree Lab Launcher: user guide

## 1. Introduction

The oTree Lab Launcher takes a working oTree experiment and makes it run in your
lab in a few clicks. You point it at your project folder and it wires up the
database, the per-seat participant links, and the experiment room, then starts
the oTree server ready for participants to sit down and begin.

It runs as a desktop app that opens in your default browser (there is also a
simple native-window version). Two people use it: a **lab manager** who sets the
machine up once, and **experimenters** who then launch their own studies without
touching any of that setup.

## 2. Usage

### Part 1 — Lab manager (one-time setup)

The lab manager looks after the machine that runs sessions. This is a one-time
job. Once it is done, experimenters never repeat any of it: they open the
launcher, point it at their project, and launch.

The first time the launcher runs on a machine (when there is no
`data/lab_info.json` yet) it opens a **setup wizard** on its own. In the wizard
you:

- Add your lab or labs: a name, the host address of the machine that runs
  sessions, and the seat list.
- Optionally set up a shared Postgres database and the Postgres admin username
  and password (see the tiers below).

Your labs, hosts, seat lists, and room-map references are saved to
`data/lab_info.json` (`data/lab_info.example.json` shows the shape). You rarely
need to hand-edit that file, because hosts, passwords, and databases are all
editable later from inside the app under **gear -> Lab Settings**. Room layouts
(the top-down seat maps the launcher draws) live in `data/maps/`; each lab points
at a map by name, and `data/maps/README.md` explains how to add your own.

**What depends on the setup: three tiers**

Which features are available to experimenters depends on how far the lab manager
takes the setup.

1. **SQLite — always works, zero setup.** oTree's built-in SQLite database needs
   no configuration at all. It is the option that always works out of the box,
   and the whole session runs on the local machine. Any experimenter can use it
   with nothing set up by the lab manager. In the app it is the
   **"No lab database (oTree SQLite)"** option. If you do nothing else, this
   still works.

2. **Postgres — for a shared or custom database.** To offer a shared default
   database that every experimenter can pick in one click, and/or to let
   experimenters point at their own custom Postgres databases, the lab manager
   must install and set up PostgreSQL on the machine and enter the **Postgres
   admin username and password** in the setup wizard. With that done, the wizard
   can create the shared "easy-use" database, and the in-app buttons let anyone
   create their own database later, so you do not have to make one per study.
   (Creating a database from the app also needs `psycopg2`:
   `pip install psycopg2-binary`.)
   **Databases stay on this computer by default.** Settings -> Custom databases
   has a lab setting, **"Databases on this computer only (localhost)"**, which is
   **on** for new and existing installs: every database Host is `localhost`, with
   no pen to change it. It is saved in `lab_info.json`, so it applies to the
   whole lab.
   **To use a database on another computer, untick that box first.** It can then
   be added, but only registered, not created. In the add-database dialog the
   **Host** shows `localhost` greyed out; **click the pen at the end of the box**
   to type another IP or hostname (the Port unlocks with it). As soon as the host
   is not this computer, **"The database already exists in Postgres"** is ticked
   and locked: the database must already exist there, and launches then connect
   to that host. Switching back to `localhost` brings the create option back.
   A database on another computer is always shown with its address in grey
   (e.g. *on 192.0.2.10:5432*) in the database picker, on the Database card and
   on the launch confirmation screen, so it is obvious the session uses another
   computer. That computer's firewall and Postgres (`listen_addresses`,
   `pg_hba.conf`) must allow the connection; test it with a plain `psql -h`
   first.

3. **GitHub workspaces (Organisation Sync) — for pulling experiment folders from
   your lab's GitHub organisation.** This is optional and **off by default**. To
   let experimenters clone and update experiment repos straight from your lab's
   GitHub organisation (no terminal), the lab manager must:
   - **Create a GitHub organisation** for the lab and **set its base member
     permission to Read**, so members can clone and pull but not push. Make the
     lab's own GitHub account a member and allow fine-grained personal access
     tokens in the organisation settings.
   - **Give each shared lab PC one read-only lab token (recommended).** Using the
     lab account, create ONE fine-grained token for the organisation with
     **Contents: Read-only** on **All repositories**, and paste it once on each
     lab PC as the password on the first clone; it is stored in Windows
     Credential Manager / the macOS Keychain. It can only read the lab's code,
     belongs to the lab (not a person), and leaves no personal login on a shared
     PC. Renew it before it expires. (Simpler alternative: sign the lab account
     in on each PC. **Never** use a personal account on a shared PC.)
   - **Researchers' own computers:** sign in with their own GitHub account.

   **The full step-by-step setup is in
   [GitHub Organisation Sync: setup and use](github_org_sync.md)** (organisation
   settings, the lab token, signing in and out on Windows and macOS, publishing a study into
   the organisation, moving repos between organisations, troubleshooting).
   **The launcher never stores or handles any token or GitHub login itself**; it
   relies entirely on the login already stored on the machine. If that is not set up,
   leave the feature off.

   Turn it on under **gear -> Lab Settings -> GitHub Organisation Sync**: a tick
   box shows or hides the GitHub buttons, and a field sets the organisation name
   (so the clone target is `<org>/<repo>` and is not hardcoded). Both are lab
   settings, saved with the rest of the lab setup in `data/lab_info.json` (not
   with the per-user light/dark theme). When it is on, experimenters get a **GitHub**
   button next to Browse (clones a named repo and auto-selects it; its folder
   picker opens in the launcher's git-ignored `local/` scratch folder) and a
   per-config **Git Pull** button (runs `git pull` in the selected study folder,
   never the launcher's own folder).

   **What the experimenter sees.** Both buttons report back in plain language,
   with the key result in bold and git's own output tucked into a collapsed
   **Git output** block (open it only when troubleshooting):

   - **Clone.** Pressing **Clone** keeps the dialog open and shows
     *Checking that `<org>/<repo>` exists…* then *Cloning…*. On success the
     dialog closes and the new folder is selected as the study folder. If
     something is wrong the dialog **stays open with the name still typed in**,
     shows the reason, and the button becomes **Retry**. Typical reasons: *No
     repository called X found in ORG, or this computer's GitHub login cannot
     see it. Check the name.* (GitHub answers the same way for a repository
     that does not exist and a private one this PC has no access to, so check
     the spelling first, then the PC's GitHub login); a folder with that name
     already exists in the chosen parent folder; GitHub could not be reached
     (no network); GitHub did not accept this PC's login; or the organisation
     name is not set yet.
   - **Git Pull.** After every pull a result appears at the bottom of the
     project status box, one of three:
     - **Nothing new: already up to date.**
     - **Pulled N changed files.** with the date and subject of the latest
       commit that came in, and a collapsed **Changes pulled from Git** list
       showing each file as added / modified / deleted with its lines added and
       removed.
     - **Git pull failed** with the reason, for example: no network; GitHub did
       not accept this PC's login; *local changes to `app/pages.py` would be
       overwritten* (the files are named, and nothing was changed); or the
       folder is not a git repo.

     The result stays while that study folder is selected and clears when you
     choose another folder.

**The per-seat links (the important part)**

Each lab PC opens its own fixed link:

```
http://HOST:8000/room/study?participant_label=SEAT&welcome_page_ok=1
```

- `HOST` is the machine that runs the session (the one running the launcher).
- `study` is the room.
- `SEAT` is that computer's seat label.
- `welcome_page_ok=1` skips oTree 6's Welcome/Start page, so the seat auto-admits
  with no click.

**This exact URL (with `welcome_page_ok=1`) is the link to put in all lab
documentation and on the lab computers.** Without the flag, oTree 6 first shows a
Welcome page that needs a click. Set each machine's link once — as the browser
homepage or a bookmark, one seat per machine — and you never touch it again. The
link is always active; before the room session exists it shows a wait page that
advances on its own the moment the session opens. When a participant opens their
link, that seat flips from grey to green on the experimenter's monitor, which is
how the consistent links track who's who.

**Room warning.** The desktop shortcuts point at the `study` room. If a study
uses a different room, its links must point at that room instead:

```
http://HOST:8000/room/THEIRROOM?participant_label=SEAT&welcome_page_ok=1
```

The launch confirmation screen warns about this whenever the chosen room is not
`study`, and shows the correct link to use.

**Saving configs.** You can save a config per study. A saved config relaunches
that study in one click in later sessions, so common studies are always a
double-click away.

**Updating the launcher.** In Settings, click **Update?** at the bottom; when a
new version is available, **Update** runs `git pull` on the launcher folder and
then asks you to **quit and reopen the launcher** (the Simple Tk app links to
GitHub instead; run `git pull` there). Your `data/` (labs, configs,
databases) is never touched. If the only thing in the way is a changed copy of a
**shipped template file** (`data/README.md`, `data/lab_info.example.json`,
`data/maps/README.md` or the example maps, typically after copying a whole
`data/` folder from another computer), the update puts those files back to the
shipped version, retries, and says so: *"Restored 2 shipped template files that
had been changed locally: ..."*. If any other file has local changes, **the
update stops and lists the files**; undo those edits, then update again. When
copying lab settings between computers, copy only `lab_info.json` and your own
maps, not the README or example files.

### Part 2 — Experimenter (usage)

Three things, in order. Read the first line of each, do it, and move on. The
"if you want to change things" note is optional.

**(i) Add your experiment folder.** Two ways:

- Click **Browse** and pick your oTree project folder. (You can also paste the
  folder path directly into the path box.)
- Or click the **GitHub** button to pull the folder from your lab's GitHub
  organisation. Type the repository name, choose a parent folder, and press
  **Clone**; the dialog only closes once the clone has worked. If it says no
  repository was found, check the spelling and press **Retry**.

*Note:* the GitHub option only works if your lab manager has set up
Organisation Sync. If you do not see it, or it does not work, ask your lab
manager.

**(ii) Save or pick a config.** A config is your study's saved settings (folder,
lab, database, seats). Saving one with **Save as new config...** writes it so you
can relaunch that exact study in one click later — pick it from the config list
on the left instead of setting everything up again. You can also save a headless
one-click desktop shortcut with **Save one-click shortcut** that re-runs the
launcher straight into that config.

*If you want to change things:* adjust the fields (lab, database, seats) before
saving. Saved configs are never edited in place — change a field and use **Save
as new config...** again to keep the original safe.

**(iii) Run it.** Once your config is saved or selected, click **Launch**
(bottom-right). A **"Before you launch"** screen appears with a summary and any
issues. When everything is clear it shows **Ready to launch**; click **Launch**
again to start. The server starts and the oTree dashboard opens on its own.

*If you want to change things:* if the screen lists warnings (for example a
shared database or a non-`study` room), the button reads **Launch anyway**; if it
lists must-fix items, fix them first and the button re-enables.

---

## Screens

### The main window

![The oTree Lab Launcher main window](screenshots/intro_screen.png)

*Configs on the left; add your project and pick lab / database / seats on the right; Launch at the bottom-right.*

### The default run path, step by step

On the default path you do not set up the lab, database, or seats yourself:
**Launch** uses the lab default setup your lab manager already configured on the
machine, so you just add your project and launch.

**Step 1 — add your experiment folder (Browse).**

![Adding your experiment folder with Browse](screenshots/howto_1_browse.png)

*Click Browse and pick your oTree project folder (or paste its path, or use the GitHub button).*

**Step 2 — make it lab-ready (if prompted).**

![The 'not lab-ready yet' popup with Get ready for the lab](screenshots/howto_2_makeready.png)

*If the project is not yet lab-ready, this popup appears. Click **Get ready for the lab** and the launcher wires it up for you.*

**Step 3 — launch.**

![The ready project screen, click Launch](screenshots/howto_3_launch.png)

*Once the project is recognised and ready, just click **Launch**. The server starts and the oTree dashboard opens on its own.*

The dashboard then shows the live monitor for the running session:

![The running-session monitor dashboard](screenshots/monitor_dashboard.png)

*Seats flip from grey to green as participants open their machine's link.*
