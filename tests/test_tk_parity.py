#!/usr/bin/env python3
"""Pass 4 parity tests (Codex #11 de-duplication).

The Tk launcher used to keep its OWN copies of the launch/storage/settings
logic that also lives in otree_core. Pass 4 routes the Tk copies through core so
there is one implementation. These tests are the guard against the two faces
ever drifting again: they run the SAME fixtures through the Tk entry points and
assert the result equals calling core directly (the launch env dict, the
resetdb + prodserver command specs, the settings block, and a saved-then-loaded
store round-trip).

Run with (imports the Tk module; never opens a window, so no display is needed):
    pytest tests/test_tk_parity.py

This suite imports ``otree_lab_launcher``, which imports ``tkinter`` at module
level, so it is skipped where Tk is not available (``pytest.importorskip``); it
never creates a Tk root, so it runs headless (no X display / xvfb required).
"""

import copy
import os
import sys
import tempfile
import unittest

import pytest

pytest.importorskip("tkinter")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "app")
sys.path.insert(0, APP)

import otree_core as core  # noqa: E402
import otree_lab_launcher as app  # noqa: E402


def _cfg(**over):
    c = dict(app.DEFAULT_CONFIG)
    c["project_path"] = "/tmp/proj"
    c.update(over)
    return c


# A spread of configs that exercises every branch build_env / the command
# builders take: lab DB, custom DB with URL-special creds, no DB, no seats,
# non-production, DEMO auth, blank/invalid port, edited seats.
FIXTURES = [
    _cfg(),
    _cfg(db_mode=app.DB_MODE_NONE, seat_mode=app.SEAT_NONE,
         production=False, auth_level="none"),
    _cfg(db_mode=app.DB_MODE_CUSTOM, db_user="u@x", db_password="p:s/w?d#",
         db_host="h", db_port="6543", db_name="n", auth_level="DEMO"),
    _cfg(port="", room_name="myroom", seat_mode=app.SEAT_NONE),
    _cfg(port="not-a-port", room_name="study"),
    _cfg(production=False, auth_level="none", seat_mode=app.SEAT_EDIT,
         seat_excluded=["3", "5"]),
]

# A base environment carrying stale variables build_env must strip, so the
# parity check also covers the removal branches.
BASE_ENV = {
    "PATH": "/usr/bin",
    "DATABASE_URL": "postgres://stale",
    "OTREE_PRODUCTION": "9",
    "OTREE_AUTH_LEVEL": "OLD",
    "OTREE_LAB_LABEL_FILE": "/stale/seats.txt",
}

LABEL_FILES = [None, "/tmp/seats/study.txt"]

PLATFORMS = ["win32", "darwin", "linux"]


class TestBuildEnvParity(unittest.TestCase):
    def test_build_env_matches_core(self):
        for i, cfg in enumerate(FIXTURES):
            for lf in LABEL_FILES:
                a = app.build_env(copy.deepcopy(cfg), dict(BASE_ENV), lf)
                b = core.build_env(copy.deepcopy(cfg), dict(BASE_ENV), lf)
                self.assertEqual(a, b, "build_env drift, fixture %d, label %r" % (i, lf))

    def test_launcher_env_keys_matches_core(self):
        for i, cfg in enumerate(FIXTURES):
            for lf in LABEL_FILES:
                self.assertEqual(
                    app.launcher_env_keys(cfg, lf),
                    core.launcher_env_keys(cfg, lf),
                    "launcher_env_keys drift, fixture %d, label %r" % (i, lf))

    def test_build_env_keys_agree_with_env(self):
        # The list of keys and the dict of values must still describe the same
        # run (the invariant both faces relied on), now sourced only from core.
        for cfg in FIXTURES:
            for lf in LABEL_FILES:
                env = app.build_env(cfg, dict(BASE_ENV), lf)
                for key in app.launcher_env_keys(cfg, lf):
                    self.assertIn(key, env)


class TestCommandParity(unittest.TestCase):
    def test_resetdb_command_matches_core(self):
        self.assertEqual(app.resetdb_command(), core.resetdb_command())
        self.assertEqual(app.resetdb_command(), ["otree", "resetdb"])

    def test_prodserver_argv_matches_core(self):
        for cfg in FIXTURES:
            self.assertEqual(app._prodserver_argv(cfg), core._prodserver_argv(cfg))

    def test_build_server_launch_matches_core(self):
        for i, cfg in enumerate(FIXTURES):
            env = app.build_env(cfg, dict(BASE_ENV), "/tmp/seats/study.txt")
            for plat in PLATFORMS:
                a = app.build_server_launch(cfg, "/proj path", env, plat)
                b = core.build_server_launch(cfg, "/proj path", env, plat)
                self.assertEqual(a, b, "build_server_launch drift, fixture %d, %s" % (i, plat))

    def test_macos_shell_script_matches_core(self):
        cfg = _cfg()
        env = app.build_env(cfg, dict(BASE_ENV), "/tmp/seats/study.txt")
        pairs = [(k, env[k]) for k in app.launcher_env_keys(cfg) if k in env]
        self.assertEqual(
            app.macos_shell_script(cfg, "/proj", pairs),
            core.macos_shell_script(cfg, "/proj", pairs))


class TestBlockParity(unittest.TestCase):
    def test_lab_block_is_core(self):
        self.assertEqual(app.LAB_BLOCK, core.LAB_BLOCK)
        self.assertEqual(app.BLOCK_MARKER, core.BLOCK_MARKER)
        self.assertEqual(app.BLOCK_END_MARKER, core.BLOCK_END_MARKER)

    def test_block_file_still_matches_the_constant(self):
        # The documented reference file must still match the one constant, so the
        # sync test stays meaningful after Tk points at core.
        with open(os.path.join(APP, "otree_lab_block.py"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), app.LAB_BLOCK)


class TestStoreParity(unittest.TestCase):
    def _sample_presets(self):
        # Real records (not the empty-file fallback) so both faces return the
        # stored data identically; the fallback default legitimately differs
        # (the Tk face keeps its own browser-open delay), which is not what a
        # persistence round-trip is testing.
        return [
            {"name": "Lab default", "author": "builtin", "builtin": True,
             "created": "2026-01-01T00:00:00", "last_run": None,
             **{k: v for k, v in app.DEFAULT_CONFIG.items()}},
            {"name": "Dictator", "author": "jt", "builtin": False,
             "created": "2026-02-02T00:00:00", "last_run": "2026-02-03T09:00:00",
             "project_path": "/exp/dictator", "room_name": "study"},
        ]

    def test_saved_then_loaded_round_trip_matches_core(self):
        presets = self._sample_presets()
        extra = {"lab_presets": {"small": {"host": "10.0.0.1"}}, "pg_admin": {"user": "pg"}}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "presets.json")
            # Save via the Tk path, read back via BOTH faces: identical.
            app.save_store(copy.deepcopy(presets), dict(extra), path)
            a_presets, a_extra = app.load_store(path)
            c_presets, c_extra = core.load_store(path)
            self.assertEqual(a_presets, c_presets)
            self.assertEqual(a_extra, c_extra)
            # And the round-trip preserved what went in.
            self.assertEqual(a_presets, presets)
            self.assertEqual(a_extra, extra)

    def test_tk_save_matches_core_save_byte_for_byte(self):
        presets = self._sample_presets()
        extra = {"note": "x"}
        with tempfile.TemporaryDirectory() as d:
            p_app = os.path.join(d, "a.json")
            p_core = os.path.join(d, "c.json")
            app.save_store(copy.deepcopy(presets), dict(extra), p_app)
            core.save_store(copy.deepcopy(presets), dict(extra), p_core)
            with open(p_app, encoding="utf-8") as h:
                a_text = h.read()
            with open(p_core, encoding="utf-8") as h:
                c_text = h.read()
            self.assertEqual(a_text, c_text)

    def test_tk_save_is_owner_only_on_posix(self):
        if os.name != "posix":
            self.skipTest("POSIX perms only")
        import stat
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "presets.json")
            app.save_store(self._sample_presets(), {}, path)
            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(mode, 0o600)

    def test_missing_file_uses_tk_default_preset(self):
        # The reconciled difference: the Tk face keeps its own default preset
        # (its browser-open delay) even though the file handling is shared.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "nope.json")
            presets, extra = app.load_store(path)
            self.assertEqual(len(presets), 1)
            self.assertEqual(presets[0]["wait_seconds"], app.DEFAULT_CONFIG["wait_seconds"])
            self.assertEqual(extra, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
