# oTree Lab Launcher: web-tech front end

The re-skin of the Tkinter `otree_lab_launcher.py`. Same job, same powers, a
prettier native window. Web tech for the looks; a native Python process for the
powers (full filesystem, real subprocesses).

## Run it

**Easiest:** at the repo root, double-click `Mac_Start oTree Lab Launcher.command`
(Mac) or `Win_Start oTree Lab Launcher.vbs` (Windows) - the default launcher is this
web front end. On first run they build a private virtualenv in your home folder
(`~/.otree-lab-launcher-venv`) and install pywebview into it, then launch. Nothing
touches the system Python. (The `.vbs` is the windowless Windows entry; the visible-
console `debug_...bat` variant lives in `alternative_launchers/`.)

**Manual:** don't `pip install` into a Homebrew/system Python: modern Pythons
refuse it (PEP 668, "externally-managed-environment"). Use a venv (run from the
repo root, so the paths point into `app/`):

```
python3 -m venv ~/.otree-lab-launcher-venv
~/.otree-lab-launcher-venv/bin/python -m pip install -r app/requirements-web.txt   # pinned deps
~/.otree-lab-launcher-venv/bin/python app/otree_launcher_web.py
```

On Windows 10/11 it renders through the built-in **Edge WebView2** runtime (no
Chromium bundle, no Node build) and just needs `pip install pywebview`. On macOS
it uses the system WebKit via pyobjc; on Linux use the `pywebview[gtk]`/`[qt]`
extra.

## What's here

- **`../otree_core.py`**: the pure logic (config model, `DATABASE_URL`, seat
  files, resetdb/prodserver commands, the `settings.py` block, the data-folder
  storage (`lab_info.json`, `machine.json`, `saved_configs.json`), the 1.5.0
  migration and the per-PC setup wizard logic, project validation). No tkinter, so it imports anywhere.
- **`../otree_launcher_web.py`**: the pywebview host + the `Api` bridge class.
  Every method is callable from the page as `window.pywebview.api.<name>`.
- **`index.html`**: the UI. It calls `window.pywebview.api.*` for real actions
  and, opened in a plain browser with no backend, still runs as the static
  visual preview (feature-detected).

## What the buttons really do

| UI action | `Api` method | Effect |
|-----------|--------------|--------|
| Config dropdown | `select_config` | loads that saved config's fields |
| Browse (project) | `pick_project_folder` | native folder dialog → validate → show apps |
| Browse (participant file) | `pick_participant_file` | native file dialog |
| Save config... | `save_dialog_state`, `save_config_as` | the same rules as the launch row (`core.save_config_choice`): new, or Replace (old version to Deleted configs) |
| Before you launch, save row | `launch_save_offer`, `launch(save=...)` | saves those fields first, then launches them; a failed save does not launch |
| Sidebar click, delete, Quit, window close | `unsaved_question` | Save / Discard / Cancel when the form has unsaved work |
| History > Config edits | `config_history` | read-only edit log |
| Settings > Deleted configs | `deleted_configs`, `recover_config` | the list; Recover refreshes the sidebar and leaves the form alone |
| Launch session | `launch_briefing` → `launch` | shows the per-machine instructions popup first (which lab/host, which per-seat link); on OKAY: resetdb (if ticked), starts `otree prodserver` in a new console, opens the page, then hands off to oTree and the launcher can close |
| Save one-click shortcut… | `save_shortcut` | for a SAVED, unmodified config: writes a headless one-click shortcut (`.vbs`/`.command`) that re-runs the launcher for that config; the DB password stays in `machine.json`, never in the file |
| Copy block | `settings_block_text` | copies the `settings.py` chunk for the researcher to paste themselves |
| Advanced > What happens on launch | `launch_timeline`, `move_launch_item`, `validate_script`, `validate_service` | the timeline in run order (scripts, reset, oTree, then browser pages) and below it what is not selected; core decides both lists. The page keeps only the choices: `script_choices`, `extra_scripts`, `launch_order`, `service_choices`, `extra_services`, `open_browser`. Saved with the config. `launch_caption` gives the collapsed line ("2 scripts, oTree admin + 1 service") |
| Launch result box | pushed by `_run_launch` (`report`, `headline`) | with at least one script: one row per timeline item with a mark that claims only what was checked (`core.launch_box_report`, PRINCIPLES 14); a failed script stops the launch and comes first. With no script: the browser-pages list |
| Advanced > Edit on an own script | `own_script_stored`, `edit_own_script` | on a saved config writes only that script straight into `saved_configs.json`; the page moves its dirty baseline for that script only (`patchCleanSnapshot`) |
| Settings > Additional services | `add_lab_service`, `update_lab_service`, `delete_lab_service` | per lab, in `lab_info.json` (name, web address, Include by default) |
| Settings > Launch scripts | `add_lab_script`, `update_lab_script`, `delete_lab_script` | per lab, in `lab_info.json` (name, typed text, Include by default, Hidden, default position, wait rule) |
| Get ready for the lab | `append_settings_block` | appends the lab support block to the selected project's `settings.py`, after a timestamped `.bak` backup; refuses to append twice |

The launcher writes into a researcher's own code in exactly **one** place, and
never silently: the **Get ready for the lab** button (`append_settings_block`)
appends the lab support block after a timestamped backup and refuses to append twice.
This is a deliberate reversal (2026-09-10) of the old "copy-paste only" rule;
the copy-paste path (`settings_block_text`) is kept alongside it. Everything else
about `settings.py` is still read-only detection via `inspect_settings`
(`settings_status`).

The extended block re-asserts the lab database, admin login, auth level and
production/DEBUG from the launcher's environment on top of the room + seat list,
each guarded so it is inert off the lab. See the top-level `README.md` and
`../../PRINCIPLES.md`, and `_ai/verify_extended_block.py` for the empirical proof.

The launch log streams back from Python into the Activity log via `evaluate_js`.

## Notes / follow-ups

- **Pure logic is fully shared now:** all of `otree_lab_launcher.py`'s pure logic
  delegates to `otree_core.py` (the config model — `DEFAULT_CONFIG`/`FIELD_KEYS`
  are the SAME object, not a copy — plus `build_env`, the seat/label helpers, the
  settings.py block, presets storage, the lab-preset/database/preflight layer,
  and, since 2026-09-24, the last pure-logic tail: `normalize_config`,
  `configs_differ`, the DB-URL builders, `build_url`, `validate_project`, the
  storage-path + home-lab helpers, `preset_from_fields`, `export_bat_text` and
  the rest are all `name = core.name` aliases). What remains duplicated is only
  the Tk GUI layer. `export_bat_text` (the standalone `.bat` export) is not wired
  to any button in either face — only the tests call it.
- The Save dialog uses a small inline name field in the modal (not
  `window.prompt`), so it works the same in the pywebview window and the
  browser-mode server.
- Design spec: the full design spec that bossman keeps outside this repo (ask
  bossman for the path).

## Closing with unsaved work

- **Quit and the pywebview window's close box** get the real Save / Discard / Cancel. The
  close event runs on the GUI thread and waits, so asking the page from inside it would deadlock:
  the first close is cancelled and the page asked from a worker (`Api._on_window_closing`); if the
  page does not answer in 4 s the window closes anyway.
- **A plain browser tab cannot show a custom box.** With unsaved work it gets only the browser's
  own "Leave site?" prompt (browsers fix its wording, and may skip it if the page never had a
  click), with no Save choice. The quit beacon fires on `pagehide` only, never `beforeunload`, so
  staying on the page does not stop the launcher. The beat says "hidden" while that prompt is up,
  so the heartbeat watchdog cannot stop the launcher under it.
