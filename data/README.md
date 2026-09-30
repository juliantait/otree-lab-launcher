# data/ - your config and maps live here

This folder holds everything the launcher reads and writes, so an update is
"copy the new version over the top and keep your `data/` folder". The app code
lives in `app/`; `data/` stays at the repo root.

## Shipped (in git)

- `lab_info.example.json` - a template. Copy it to `lab_info.json` and edit it
  (or let the launcher's first-run setup create `lab_info.json` for you).
- `maps/` - room maps (seat geometry, `example_small` and `example_large`). Not
  secret; drop your own `maps/<name>.json` in and point a lab at it.
- this `README.md`.

## Yours (created at runtime, not in git)

- `lab_info.json` - your labs, hosts, database and admin settings, plus the
  GitHub Organisation Sync on/off switch and organisation name.
- `presets.json` - your saved launch configs.
- `lab.local` - one word naming which lab this computer is.
- `ui_prefs.json` - your light/dark theme.
- `seats/` - generated participant-label files.
- `otree-lab-launcher.log` / `web_launcher.log` - logs.

Test studies and GitHub clones belong in `local/` at the repo root (next to
`app/` and `data/`), not in here: git ignores that whole folder, the GitHub clone
picker opens there, and the launcher creates it when first needed.

The env vars `OTREE_LAB_INFO`, `OTREE_LAB_MARKER` and
`OTREE_LAB_LAUNCHER_PRESETS` override the default locations when set.
