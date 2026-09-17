# oTree Lab Launcher — web-tech front end

The re-skin of the Tkinter `otree_lab_launcher.py`. Same job, same powers, a
prettier native window. Web tech for the looks; a native Python process for the
powers (full filesystem, real subprocesses).

## Run it

**Easiest:** double-click `Start oTree Lab Launcher (web).command` (Mac) or
`Start oTree Lab Launcher (web).bat` (Windows). On first run they build a
private virtualenv in your home folder (`~/.otree-lab-launcher-venv`) and
install pywebview into it, then launch. Nothing touches the system Python.

**Manual:** don't `pip install` into a Homebrew/system Python — modern Pythons
refuse it (PEP 668, "externally-managed-environment"). Use a venv:

```
python3 -m venv ~/.otree-lab-launcher-venv
~/.otree-lab-launcher-venv/bin/python -m pip install pywebview pyobjc-framework-Cocoa pyobjc-framework-WebKit   # macOS
~/.otree-lab-launcher-venv/bin/python otree_launcher_web.py
```

On Windows 10/11 it renders through the built-in **Edge WebView2** runtime (no
Chromium bundle, no Node build) and just needs `pip install pywebview`. On macOS
it uses the system WebKit via pyobjc; on Linux use the `pywebview[gtk]`/`[qt]`
extra.

## What's here

- **`../otree_core.py`** — the pure logic (config model, `DATABASE_URL`, seat
  files, resetdb/prodserver commands, the `settings.py` block, `presets.json`
  storage, project validation). No tkinter, so it imports anywhere.
- **`../otree_launcher_web.py`** — the pywebview host + the `Api` bridge class.
  Every method is callable from the page as `window.pywebview.api.<name>`.
- **`index.html`** — the UI. It calls `window.pywebview.api.*` for real actions
  and, opened in a plain browser with no backend, still runs as the static
  visual preview (feature-detected).

## What the buttons really do

| UI action | `Api` method | Effect |
|-----------|--------------|--------|
| Config dropdown | `select_config` | loads that saved config's fields |
| Browse (project) | `pick_project_folder` | native folder dialog → validate → show apps |
| Browse (participant file) | `pick_participant_file` | native file dialog |
| Save as new… | `save_config_as` | writes to `presets.json` |
| Launch session | `launch_briefing` → `launch` | shows the per-machine instructions popup first (which lab/host, which per-seat link); on OKAY: resetdb (if ticked), starts `otree prodserver` in a new console, opens the page, then hands off to oTree and the launcher can close |
| Export .bat… | `export_bat` | native save dialog → standalone `.bat` |
| Copy block | `settings_block_text` | copies the `settings.py` chunk for the researcher to paste themselves |
| Add block to settings.py | `append_settings_block` | appends the lab support block to the selected project's `settings.py`, after a timestamped `.bak` backup; refuses to append twice |

The launcher writes into a researcher's own code in exactly **one** place, and
never silently: the **Add block to settings.py** button (`append_settings_block`)
appends the lab support block after a timestamped backup and refuses to append twice.
This is a deliberate reversal (2026-09-10) of the old "copy-paste only" rule;
the copy-paste path (`settings_block_text`) is kept alongside it. Everything else
about `settings.py` is still read-only detection via `inspect_settings`
(`settings_status`).

The extended block re-asserts the lab database, admin login, auth level and
production/DEBUG from the launcher's environment on top of the room + seat list,
each guarded so it is inert off the lab. See the top-level `README.md` and
`../PRINCIPLES.md`, and `_ai/verify_extended_block.py` for the empirical proof.

The launch log streams back from Python into the Activity log via `evaluate_js`.

## Notes / follow-ups

- **Logic is currently duplicated:** `otree_core.py` is an extracted copy of the
  logic that still also lives inline in `otree_lab_launcher.py`. Next cleanup:
  have the old Tk launcher `from otree_core import *` so there is one source of
  truth. Left duplicated for now so the working Tk launcher is untouched and can
  be verified independently.
- `window.prompt` (used by "Save as new…") is supported by Edge WebView2 and
  WebKit; if a host build blocks it, swap in a small inline name field.
- Design spec: the full design spec that bossman keeps outside this repo (ask
  bossman for the path).
