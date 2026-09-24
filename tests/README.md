# tests/ — the committed test suite

These are the launcher's shipped, platform-neutral tests, run by
`.github/workflows/tests.yml` on ubuntu / windows / macOS and Python 3.11 + 3.12.
They are **stdlib-only**: the only thing CI installs is `pytest`.

Run them locally from the repo root:

```
pip install pytest
pytest tests/
```

| File | Covers | Needs |
|------|--------|-------|
| `test_core_features.py` | `otree_core`: launch-briefing caution flag, `create_database` argument handling, project-room enumeration, lab-preset storage + back-compat | stdlib only (one live-connection test self-skips without `psycopg2`) |
| `test_db_management.py` | `otree_core`: `slugify_pg_dbname`, `edit_database`, register-without-create, DB-failure recovery (`issue_fix_for` / `switch_to_lab_default`) | stdlib only |
| `test_tk_parity.py` | that the Tk launcher's pure-logic names still match `otree_core` (the guard for the item-#10 consolidation): env dict, resetdb/prodserver commands, the settings block, a store round-trip | imports `otree_lab_launcher`, so it `pytest.importorskip("tkinter")`s — skips where Tk is absent; never opens a window, so no display is needed |

The larger body of scratch tests and verification scripts lives in the
gitignored `_ai/` folder (development-only); the suites here are the subset that
is stdlib-only and meaningful to a cloner. `test_tk_parity.py` is a committed
mirror of `_ai/test_pass4_parity.py`.
