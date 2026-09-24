#!/usr/bin/env python3
"""Unit tests for the four new features, all in creed_core (no tkinter needed).

  Feature 1  the launch-briefing caution flag
  Feature 2  create_database argument handling + error mapping (the full live
             red/green matrix is in verify_create_database.py)
  Feature 3  project room enumeration, with fake projects and every fallback
  Feature 4  lab-preset storage + backward-compat mapping

Run:  python3 _ai/test_core_features.py
(psycopg2 is optional; the one test that needs a live connection attempt is
skipped when it is not installed.)
"""
import os
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
import otree_core as core  # noqa: E402

try:
    import psycopg2 as _psycopg2  # noqa: F401
    HAS_PSYCOPG2 = True
except Exception:
    HAS_PSYCOPG2 = False

try:
    import psycopg2  # noqa: F401
    HAVE_PSYCOPG2 = True
except ImportError:
    HAVE_PSYCOPG2 = False


# ---------------------------------------------------------------------------
# Feature 4: lab presets
# ---------------------------------------------------------------------------
class TestLabPresets(unittest.TestCase):
    def test_seed_recreates_the_two_labs_exactly(self):
        presets = core.default_lab_presets()
        by_id = {p["id"]: p for p in presets}
        self.assertEqual(by_id["small"]["ip"], "145.18.178.133")
        self.assertEqual(by_id["large"]["ip"], "145.18.178.130")
        self.assertEqual(len(by_id["small"]["seats"]), 22)
        self.assertEqual(len(by_id["large"]["seats"]), 31)
        self.assertEqual(by_id["small"]["seats"], [str(_i) for _i in range(1, 23)])
        self.assertEqual(by_id["large"]["seats"], ["A1", "A2", "A3", "A5", "A6", "A7", "A8", "B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "D1", "D2", "D3", "D4", "E1", "E2", "E3", "E4"])
        self.assertTrue(all(p["display"] for p in presets))
        self.assertTrue(all(p["builtin"] for p in presets))

    def test_empty_store_seeds_builtins(self):
        self.assertEqual([p["id"] for p in core.lab_presets_from_store({})],
                         ["small", "large"])
        self.assertEqual([p["id"] for p in core.lab_presets_from_store({"lab_presets": []})],
                         ["small", "large"])

    def test_backward_compat_old_ids_resolve(self):
        presets = core.lab_presets_from_store({})
        # A config saved with lab="small"/"large" still resolves to host+seats.
        self.assertEqual(core.resolve_host({"lab": "small"}, presets), "145.18.178.133")
        self.assertEqual(core.resolve_host({"lab": "large"}, presets), "145.18.178.130")
        self.assertEqual(len(core.lab_default_seats({"lab": "small"}, presets)), 22)
        self.assertEqual(len(core.lab_default_seats({"lab": "large"}, presets)), 31)

    def test_old_ids_resolve_even_with_no_presets_passed(self):
        # The host/seat helpers keep their old hardcoded fallback when no preset
        # list is threaded in, so every legacy caller is unaffected.
        self.assertEqual(core.resolve_host({"lab": "small"}), "145.18.178.133")
        self.assertEqual(len(core.lab_default_seats({"lab": "large"})), 31)

    def test_normalize_no_longer_clobbers_a_preset_id(self):
        cfg = core.normalize_config({"lab": "rotterdam-annex"})
        self.assertEqual(cfg["lab"], "rotterdam-annex")
        # but a blank lab still falls back to the default
        self.assertEqual(core.normalize_config({"lab": ""})["lab"], core.DEFAULT_CONFIG["lab"])

    def test_add_new_lab_and_resolve(self):
        presets = core.default_lab_presets()
        ok, msg, new_list, preset = core.add_lab_preset(presets, "Rotterdam Annex",
                                                        "10.0.0.9", "1\n2, 3 4")
        self.assertTrue(ok, msg)
        self.assertEqual(preset["seats"], ["1", "2", "3", "4"])
        self.assertEqual(preset["geometry"], core.LAB_GEO_GRID)
        self.assertFalse(preset["builtin"])
        self.assertEqual(core.resolve_host({"lab": preset["id"]}, new_list), "10.0.0.9")
        self.assertEqual(core.lab_default_seats({"lab": preset["id"]}, new_list),
                         ["1", "2", "3", "4"])

    def test_add_rejects_bad_seats_and_blank_fields(self):
        presets = core.default_lab_presets()
        ok, msg, _l, _p = core.add_lab_preset(presets, "X", "1.2.3.4", "seat one!")
        self.assertFalse(ok)
        self.assertIn("valid participant labels", msg)
        ok2, _m, _l, _p = core.add_lab_preset(presets, "", "1.2.3.4", "1 2")
        self.assertFalse(ok2)
        ok3, _m, _l, _p = core.add_lab_preset(presets, "Named", "", "1 2")
        self.assertFalse(ok3)

    def test_add_makes_unique_ids(self):
        presets = core.default_lab_presets()
        _o, _m, presets, p1 = core.add_lab_preset(presets, "Small lab", "1.1.1.1", "1 2")
        _o, _m, presets, p2 = core.add_lab_preset(presets, "Small lab", "2.2.2.2", "1 2")
        self.assertNotEqual(p1["id"], p2["id"])
        self.assertNotEqual(p1["id"], "small")  # doesn't collide with the builtin

    def test_edit_lab(self):
        presets = core.default_lab_presets()
        ok, msg, new_list, preset = core.update_lab_preset(presets, "small",
                                                          ip="9.9.9.9", seats="1 2 3")
        self.assertTrue(ok, msg)
        self.assertEqual(core.resolve_host({"lab": "small"}, new_list), "9.9.9.9")
        self.assertEqual(core.lab_default_seats({"lab": "small"}, new_list), ["1", "2", "3"])

    def test_hide_and_show(self):
        presets = core.default_lab_presets()
        ok, msg, new_list = core.set_lab_display(presets, "small", False)
        self.assertTrue(ok, msg)
        self.assertEqual([p["id"] for p in core.displayed_lab_presets(new_list)], ["large"])
        # cannot hide the last remaining displayed lab
        ok2, msg2, _l = core.set_lab_display(new_list, "large", False)
        self.assertFalse(ok2)
        self.assertIn("stay visible", msg2)

    def test_delete_guarded_and_reselect(self):
        presets = core.default_lab_presets()
        ok, msg, new_list, next_sel = core.delete_lab_preset(presets, "small",
                                                            selected_lab="small")
        self.assertTrue(ok, msg)
        self.assertEqual([p["id"] for p in new_list], ["large"])
        self.assertEqual(next_sel, "large")  # switched off the deleted one
        # deleting the last lab is refused
        ok2, msg2, _l, _s = core.delete_lab_preset(new_list, "large", selected_lab="large")
        self.assertFalse(ok2)
        self.assertIn("cannot be deleted", msg2)

    def test_single_displayed_lab_is_forced_default(self):
        presets = core.default_lab_presets()
        _o, _m, presets = core.set_lab_display(presets, "large", False)
        # only "small" is displayed now
        self.assertEqual([p["id"] for p in core.selectable_lab_presets(presets)], ["small"])
        # even if a stale config points at "large", the selector forces "small"
        self.assertEqual(core.default_selected_lab(presets, current="large"), "small")

    def test_selectable_falls_back_when_none_displayed(self):
        # A hand-edited store with everything hidden must not empty the selector.
        presets = [dict(p, display=False) for p in core.default_lab_presets()]
        shown = core.selectable_lab_presets(presets)
        self.assertEqual(sorted(p["id"] for p in shown), ["large", "small"])

    def test_default_selected_keeps_valid_current(self):
        presets = core.default_lab_presets()
        self.assertEqual(core.default_selected_lab(presets, current="large"), "large")
        self.assertEqual(core.default_selected_lab(presets, current="bogus"), "small")

    def test_find_candidate_label_files(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "participant_labels.txt"), "w").close()
            open(os.path.join(d, "readme.md"), "w").close()
            os.makedirs(os.path.join(d, "sub"))
            open(os.path.join(d, "sub", "more.txt"), "w").close()
            os.makedirs(os.path.join(d, "__pycache__"))
            open(os.path.join(d, "__pycache__", "junk.txt"), "w").close()
            found = [os.path.relpath(p, d) for p in core.find_candidate_label_files(d)]
            self.assertIn("participant_labels.txt", found)
            self.assertIn(os.path.join("sub", "more.txt"), found)
            self.assertNotIn("readme.md", found)          # not a .txt
            self.assertNotIn(os.path.join("__pycache__", "junk.txt"), found)  # skipped dir

    def test_seat_file_selection_round_trips_through_a_saved_config(self):
        # The chosen participant file (seat_mode + seat_file) is saved as part of
        # the config and restored on reload, like every other field.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "presets.json")
            seat_file = os.path.join(d, "my_labels.txt")
            fields = dict(core.DEFAULT_CONFIG)
            fields.update({"seat_mode": core.SEAT_FILE, "seat_file": seat_file})
            preset = core.preset_from_fields("With a file", fields)
            self.assertEqual(preset["seat_mode"], core.SEAT_FILE)
            self.assertEqual(preset["seat_file"], seat_file)
            core.save_store([preset], {}, path)
            loaded, _extra = core.load_store(path)
            restored = core.normalize_config(loaded[-1])
            self.assertEqual(restored["seat_mode"], core.SEAT_FILE)
            self.assertEqual(restored["seat_file"], seat_file)

    def test_presets_round_trip_through_store(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "presets.json")
            _o, _m, new_list, preset = core.add_lab_preset(core.default_lab_presets(),
                                                          "Annex", "10.0.0.5", "1 2 3")
            core.save_store([core.default_preset()], {"lab_presets": new_list}, path)
            _presets, extra = core.load_store(path)
            loaded = core.lab_presets_from_store(extra)
            self.assertIn(preset["id"], [p["id"] for p in loaded])
            self.assertEqual(core.resolve_host({"lab": preset["id"]}, loaded), "10.0.0.5")


# ---------------------------------------------------------------------------
# Feature 1: the caution flag
# ---------------------------------------------------------------------------
class TestCautionFlag(unittest.TestCase):
    def test_creed_default_raises_caution(self):
        b = core.launch_briefing({"db_mode": core.DB_MODE_LAB, "lab": "small",
                                  "seat_mode": core.SEAT_DEFAULT})
        self.assertTrue(b["caution"])
        self.assertIn("shared lab database", b["caution_text"])
        self.assertNotIn("—", b["caution_text"])   # no em dash
        self.assertNotIn(" - ", b["caution_text"])  # no hyphen-as-dash either

    def test_custom_and_none_do_not(self):
        for mode in (core.DB_MODE_CUSTOM, core.DB_MODE_NONE):
            b = core.launch_briefing({"db_mode": mode, "lab": "large"})
            self.assertFalse(b["caution"])
            self.assertEqual(b["caution_text"], "")

    def test_configs_differ_can_ignore_project_path(self):
        base = dict(core.DEFAULT_CONFIG)
        changed = dict(base, project_path="/some/where/else")
        self.assertTrue(core.configs_differ(base, changed))
        self.assertFalse(core.configs_differ(base, changed, ignore=("project_path",)))
        # a real change still registers even with the ignore
        other = dict(base, project_path="/x", admin_username="someone")
        self.assertTrue(core.configs_differ(base, other, ignore=("project_path",)))

    def test_shortcut_name_convention(self):
        self.assertEqual(core.study_shortcut_name("Large lab"), "Study Room Large Lab")
        self.assertEqual(core.study_shortcut_name("Small lab"), "Study Room Small Lab")
        self.assertEqual(core.study_shortcut_name("Rotterdam Annex"), "Study Room Rotterdam Annex")

    def test_briefing_default_room_names_the_shortcut(self):
        b = core.launch_briefing({"db_mode": core.DB_MODE_CUSTOM, "lab": "small",
                                  "seat_mode": core.SEAT_DEFAULT, "room_name": "study"})
        self.assertTrue(b["has_participant_links"])
        self.assertEqual(b["shortcut_name"], "Study Room Small Lab")

    def test_briefing_other_room_has_no_links(self):
        b = core.launch_briefing({"db_mode": core.DB_MODE_CUSTOM, "lab": "large",
                                  "room_name": "pilot", "seat_mode": core.SEAT_DEFAULT})
        self.assertFalse(b["has_participant_links"])
        self.assertIn("/room/pilot", b["link_template"])

    def test_natural_seat_sort(self):
        self.assertEqual(core.sorted_seats(["10", "2", "1"]), ["1", "2", "10"])
        self.assertEqual(core.sorted_seats(["B1", "A2", "A1"]), ["A1", "A2", "B1"])
        self.assertEqual(core.sorted_seats(["A10", "A2", "A1"]), ["A1", "A2", "A10"])

    def test_participant_link_rooms(self):
        # The lab-shortcut default room has participant PC links; others do not.
        self.assertTrue(core.room_has_participant_links(core.DEFAULT_ROOM_NAME))
        self.assertTrue(core.room_has_participant_links("study"))
        self.assertFalse(core.room_has_participant_links("pilot"))
        self.assertFalse(core.room_has_participant_links(""))
        self.assertIn(core.DEFAULT_ROOM_NAME, core.PARTICIPANT_LINK_ROOMS)

    def test_briefing_still_has_the_link_and_room_warning(self):
        b = core.launch_briefing({"db_mode": core.DB_MODE_CUSTOM, "lab": "small",
                                  "room_name": "otherroom", "seat_mode": core.SEAT_DEFAULT})
        self.assertFalse(b["is_study"])
        self.assertIn("/room/otherroom", b["link_template"])
        self.assertEqual(b["host"], "145.18.178.133")


# ---------------------------------------------------------------------------
# Feature 3: room enumeration
# ---------------------------------------------------------------------------
def _make_project(tmp, settings_body):
    os.makedirs(tmp, exist_ok=True)
    with open(os.path.join(tmp, "settings.py"), "w") as fh:
        fh.write(textwrap.dedent(settings_body))
    return tmp


class TestRoomEnumeration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="creed-rooms-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_static_rooms(self):
        p = _make_project(os.path.join(self._tmp, "static"), """
            ROOMS = [
                dict(name='study', display_name='Study'),
                dict(name='pilot', display_name='Pilot'),
            ]
        """)
        r = core.enumerate_project_rooms(p)
        self.assertTrue(r["ok"], r["error"])
        self.assertEqual(r["rooms"], ["study", "pilot"])
        self.assertFalse(r["empty"])

    def test_dynamically_built_rooms(self):
        # A regex parse would miss these; importing resolves them.
        p = _make_project(os.path.join(self._tmp, "dyn"), """
            ROOMS = []
            for _i in range(3):
                ROOMS.append(dict(name='wave%d' % _i, display_name='Wave %d' % _i))
        """)
        r = core.enumerate_project_rooms(p)
        self.assertTrue(r["ok"], r["error"])
        self.assertEqual(r["rooms"], ["wave0", "wave1", "wave2"])

    def test_empty_rooms_is_a_real_answer_not_a_failure(self):
        p = _make_project(os.path.join(self._tmp, "empty"), """
            ROOMS = []
        """)
        r = core.enumerate_project_rooms(p)
        self.assertTrue(r["ok"], r["error"])
        self.assertEqual(r["rooms"], [])
        self.assertTrue(r["empty"])

    def test_no_rooms_defined_at_all(self):
        p = _make_project(os.path.join(self._tmp, "norooms"), """
            SESSION_CONFIGS = []
        """)
        r = core.enumerate_project_rooms(p)
        self.assertTrue(r["ok"], r["error"])
        self.assertEqual(r["rooms"], [])
        self.assertTrue(r["empty"])

    def test_import_failure_falls_back_with_a_reason(self):
        p = _make_project(os.path.join(self._tmp, "broken"), """
            raise RuntimeError('boom in settings')
        """)
        r = core.enumerate_project_rooms(p)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "import_failed")
        self.assertIn("boom", r["error"])

    def test_missing_settings(self):
        p = os.path.join(self._tmp, "nofolder_settings")
        os.makedirs(p)
        r = core.enumerate_project_rooms(p)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "no_settings")

    def test_no_project_path(self):
        r = core.enumerate_project_rooms("")
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "no_project")

    def test_timeout_kills_only_the_subprocess(self):
        p = _make_project(os.path.join(self._tmp, "slow"), """
            import time
            time.sleep(30)
            ROOMS = []
        """)
        r = core.enumerate_project_rooms(p, timeout=1.0)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "timeout")

    def test_printing_project_does_not_confuse_the_parse(self):
        p = _make_project(os.path.join(self._tmp, "noisy"), """
            print('hello from settings, doing setup work')
            print('___OTREE_ROOMS_JSON___["not", "real"]')  # a decoy line
            ROOMS = [dict(name='real_room')]
        """)
        r = core.enumerate_project_rooms(p)
        self.assertTrue(r["ok"], r["error"])
        self.assertEqual(r["rooms"], ["real_room"])

    def test_no_creed_env_leaks_into_the_probe(self):
        # Even with a CREED_* var set in this process, the probe imports clean.
        os.environ["OTREE_LAB_LABEL_FILE"] = "/tmp/should-not-matter"
        try:
            p = _make_project(os.path.join(self._tmp, "clean"), """
                import os
                assert 'OTREE_LAB_LABEL_FILE' not in os.environ, 'creed var leaked!'
                ROOMS = [dict(name='clean_room')]
            """)
            r = core.enumerate_project_rooms(p)
            self.assertTrue(r["ok"], r["error"])
            self.assertEqual(r["rooms"], ["clean_room"])
        finally:
            os.environ.pop("OTREE_LAB_LABEL_FILE", None)


# ---------------------------------------------------------------------------
# Feature 2: create_database argument handling + error mapping (no live server)
# ---------------------------------------------------------------------------
class TestCreateDatabaseArgs(unittest.TestCase):
    FULL_ADMIN = {"admin_username": "pg", "admin_password": "pw",
                  "admin_host": "127.0.0.1", "admin_port": "5432"}

    def test_blocked_when_admin_incomplete(self):
        for key in core.PG_ADMIN_KEYS:
            admin = dict(self.FULL_ADMIN)
            admin[key] = ""
            r = core.create_database(admin, "somedb")
            self.assertFalse(r["ok"])
            self.assertEqual(r["reason"], "admin_missing")
            self.assertIsNone(r["fields"])

    def test_pg_admin_ready_lists_missing(self):
        self.assertEqual(core.pg_admin_ready(self.FULL_ADMIN), [])
        self.assertIn("password", core.pg_admin_ready(
            {"admin_username": "u", "admin_password": "", "admin_host": "h", "admin_port": "1"}))

    @unittest.skipUnless(HAS_PSYCOPG2, "psycopg2 not installed")
    def test_invalid_db_name_rejected_before_any_connection(self):
        for bad in ("bad name", "1leading", "has-dash", "select", "x" * 64, ""):
            r = core.create_database(self.FULL_ADMIN, bad)
            self.assertFalse(r["ok"], bad)
            self.assertEqual(r["reason"], "bad_db_name", bad)
            self.assertIsNone(r["fields"])

    @unittest.skipUnless(HAS_PSYCOPG2, "psycopg2 not installed")
    def test_invalid_user_name_rejected(self):
        r = core.create_database(self.FULL_ADMIN, "gooddb", new_user="bad user")
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "bad_user_name")

    def test_identifier_validation_accepts_good_names(self):
        for good in ("wave2", "study_db", "_leading", "a$b", "X"):
            ok, _msg = core.validate_pg_identifier(good)
            self.assertTrue(ok, good)

    def test_missing_psycopg2_is_a_plain_message(self):
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name == "psycopg2" or name.startswith("psycopg2."):
                raise ImportError("no psycopg2 here")
            return real_import(name, *a, **k)

        builtins.__import__ = fake_import
        try:
            r = core.create_database(self.FULL_ADMIN, "gooddb")
        finally:
            builtins.__import__ = real_import
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "no_psycopg2")
        self.assertIn("psycopg2", r["message"])
        self.assertNotIn("Traceback", r["message"])

    @unittest.skipUnless(HAVE_PSYCOPG2, "psycopg2 not installed")
    def test_connection_refused_maps_cleanly(self):
        # Point at a port with nothing listening: a real, copyable error.
        admin = {"admin_username": "pg", "admin_password": "pw",
                 "admin_host": "127.0.0.1", "admin_port": "1"}
        r = core.create_database(admin, "gooddb")
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "admin_connect_failed")
        self.assertIsNone(r["fields"])
        self.assertNotIn("Traceback", r["message"])


# ---------------------------------------------------------------------------
# Round 5: per-machine lab identity (lab.local) + first-run/UI-settable flow
# ---------------------------------------------------------------------------
class TestLabIdentityMarker(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._marker = os.path.join(self._dir, "lab.local")
        self._old = os.environ.get("OTREE_LAB_MARKER")
        os.environ["OTREE_LAB_MARKER"] = self._marker

    def tearDown(self):
        import shutil
        if self._old is None:
            os.environ.pop("OTREE_LAB_MARKER", None)
        else:
            os.environ["OTREE_LAB_MARKER"] = self._old
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_unset_marker_is_none(self):
        self.assertIsNone(core.read_lab_marker())

    def test_write_then_read_builtin_ids(self):
        self.assertTrue(core.write_lab_marker("small"))
        self.assertEqual(core.read_lab_marker(), "small")

    def test_write_refuses_to_clobber(self):
        self.assertTrue(core.write_lab_marker("large"))
        # First-run write refuses when a marker already exists.
        self.assertFalse(core.write_lab_marker("small"))
        self.assertEqual(core.read_lab_marker(), "large")

    def test_set_marker_overwrites_and_takes_any_id(self):
        core.set_lab_marker("large")
        # An added lab's id (a slug) is a valid identity now, not just large/small.
        core.set_lab_marker("rotterdam-annex")
        self.assertEqual(core.read_lab_marker(), "rotterdam-annex")

    def test_blank_id_is_refused(self):
        with self.assertRaises(ValueError):
            core.set_lab_marker("")

    def test_apply_marker_points_only_the_builtin(self):
        presets = [core.default_preset(),
                   core.preset_from_fields("mine", {"lab": "small"}, author="ana")]
        core.apply_lab_marker(presets, "some-lab")
        self.assertEqual(presets[0]["lab"], "some-lab")   # built-in follows the machine
        self.assertEqual(presets[1]["lab"], "small")  # user config untouched

    def test_apply_marker_noop_when_unset(self):
        presets = [core.default_preset()]
        before = presets[0]["lab"]
        core.apply_lab_marker(presets, None)
        self.assertEqual(presets[0]["lab"], before)


class TestApplyLabIdentity(unittest.TestCase):
    def test_shows_only_the_chosen_lab(self):
        labs = core.default_lab_presets()
        ok, _msg, new_list = core.apply_lab_identity(labs, "small")
        self.assertTrue(ok)
        shown = {p["id"] for p in core.displayed_lab_presets(new_list)}
        self.assertEqual(shown, {"small"})
        # And the main selector then forces that single lab.
        self.assertEqual(core.default_selected_lab(new_list, current="large"), "small")

    def test_refuses_unknown_id(self):
        labs = core.default_lab_presets()
        ok, msg, new_list = core.apply_lab_identity(labs, "nope")
        self.assertFalse(ok)
        self.assertTrue(msg)
        # The list is returned unchanged (both still displayed).
        self.assertEqual(len(core.displayed_lab_presets(new_list)), 2)

    def test_added_lab_can_become_the_identity(self):
        ok, _m, labs, preset = core.add_lab_preset(
            core.default_lab_presets(), "Annex", "10.0.0.9", "1 2 3")
        self.assertTrue(ok)
        ok2, _m2, new_list = core.apply_lab_identity(labs, preset["id"])
        self.assertTrue(ok2)
        self.assertEqual([p["id"] for p in core.displayed_lab_presets(new_list)], [preset["id"]])


class TestCredentialedURL(unittest.TestCase):
    def test_embeds_and_encodes(self):
        url = core.credentialed_url("http://localhost:8000/rooms", "admin", "s3cr#t p@ss")
        self.assertEqual(url, "http://admin:s3cr%23t%20p%40ss@localhost:8000/rooms")

    def test_username_only(self):
        self.assertEqual(core.credentialed_url("http://h:8000/x", "admin", ""),
                         "http://admin@h:8000/x")

    def test_blank_credentials_return_unchanged(self):
        self.assertEqual(core.credentialed_url("http://h:8000/x", "", ""), "http://h:8000/x")

    def test_replaces_existing_userinfo(self):
        url = core.credentialed_url("http://old:pw@h:8000/x", "new", "np")
        self.assertEqual(url, "http://new:np@h:8000/x")

    def test_mask_hides_only_the_password(self):
        url = core.credentialed_url("http://h:8000/x", "admin", "secret")
        masked = core.mask_credentialed_url(url)
        self.assertIn("admin:", masked)
        self.assertNotIn("secret", masked)
        self.assertIn(core.MASKED_PASSWORD, masked)

    def test_mask_leaves_a_plain_url_alone(self):
        self.assertEqual(core.mask_credentialed_url("http://h:8000/x"), "http://h:8000/x")


# ---------------------------------------------------------------------------
# Preflight gate (checks 1-4). The pure-logic branches live here; the full
# red/green against a REAL pgserver Postgres is in _ai/verify_preflight.py
# (-> _ai/verify_preflight.log), which cannot run without a live DB.
# ---------------------------------------------------------------------------
class TestPreflight(unittest.TestCase):
    def _cfg(self, **over):
        cfg = {"db_mode": core.DB_MODE_NONE, "port": "8000",
               "project_path": "", "room_name": "study"}
        cfg.update(over)
        return cfg

    # --- Check 1: database (non-live branches) ---
    def test_database_skipped_for_sqlite(self):
        r = core.preflight_check_database(self._cfg(db_mode=core.DB_MODE_NONE))
        self.assertTrue(r["ok"])
        self.assertEqual(r["check"], "database")

    def test_database_unreachable_is_soft_fail(self):
        if not HAS_PSYCOPG2:
            self.skipTest("psycopg2 not installed")
        # Nothing is listening here, so connect fails cleanly (not a crash).
        cfg = self._cfg(db_mode=core.DB_MODE_CUSTOM, db_name="x", db_user="u",
                        db_password="p", db_host="127.0.0.1", db_port="1")
        r = core.preflight_check_database(cfg, timeout=2)
        self.assertFalse(r["ok"])
        self.assertIn("127.0.0.1:1", r["message"])

    # --- Check 2: launch port free ---
    def test_port_in_use_is_flagged(self):
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        try:
            r = core.preflight_check_port(self._cfg(port=str(port)))
            self.assertFalse(r["ok"])
            self.assertIn("in use", r["message"])
        finally:
            s.close()

    def test_free_port_is_ok(self):
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        r = core.preflight_check_port(self._cfg(port=str(port)))
        self.assertTrue(r["ok"])

    # --- Check 3: project sanity ---
    def test_project_missing_settings_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = core.preflight_check_project(self._cfg(project_path=tmp))
            self.assertFalse(r["ok"])
            self.assertIn("No settings.py", r["message"])

    def test_project_room_membership(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "settings.py"), "w") as fh:
                fh.write("ROOMS = [dict(name='study', display_name='Study')]\n")
            ok = core.preflight_check_project(
                self._cfg(project_path=tmp, room_name="study"))
            self.assertTrue(ok["ok"])
            bad = core.preflight_check_project(
                self._cfg(project_path=tmp, room_name="ghost"))
            self.assertFalse(bad["ok"])
            self.assertIn("ghost", bad["message"])

    # --- Check 4: oTree available (both branches, via monkeypatch) ---
    def test_otree_available_branches(self):
        real = core.otree_available
        try:
            core.otree_available = lambda: True
            self.assertTrue(core.preflight_check_otree(self._cfg())["ok"])
            core.otree_available = lambda: False
            r = core.preflight_check_otree(self._cfg())
            self.assertFalse(r["ok"])
            self.assertIn("not installed", r["message"])
        finally:
            core.otree_available = real

    # --- Check 5: psycopg2 present when Postgres is chosen ---
    def test_psycopg2_skipped_for_sqlite(self):
        # SQLite / no-database must NEVER trigger the psycopg2 warning, even when
        # the driver is genuinely absent.
        real = core.psycopg2_available
        try:
            core.psycopg2_available = lambda: False
            r = core.preflight_check_psycopg2(self._cfg(db_mode=core.DB_MODE_NONE))
            self.assertTrue(r["ok"])
            self.assertEqual(r["check"], "psycopg2")
        finally:
            core.psycopg2_available = real

    def test_psycopg2_missing_for_postgres_is_flagged(self):
        real = core.psycopg2_available
        try:
            core.psycopg2_available = lambda: False
            for mode in (core.DB_MODE_LAB, core.DB_MODE_CUSTOM):
                r = core.preflight_check_psycopg2(self._cfg(db_mode=mode))
                self.assertFalse(r["ok"], mode)
                self.assertIn("psycopg2", r["message"])
                self.assertIn("pip install psycopg2-binary", r["message"])
        finally:
            core.psycopg2_available = real

    def test_psycopg2_present_for_postgres_is_ok(self):
        real = core.psycopg2_available
        try:
            core.psycopg2_available = lambda: True
            r = core.preflight_check_psycopg2(self._cfg(db_mode=core.DB_MODE_CUSTOM))
            self.assertTrue(r["ok"])
        finally:
            core.psycopg2_available = real

    # --- Aggregation ---
    def test_aggregation_all_pass_and_mixed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "settings.py"), "w") as fh:
                fh.write("ROOMS = [dict(name='study', display_name='Study')]\n")
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", 0))
            free = s.getsockname()[1]
            s.close()
            real = core.otree_available
            try:
                core.otree_available = lambda: True
                good = self._cfg(db_mode=core.DB_MODE_NONE, port=str(free),
                                 project_path=tmp, room_name="study")
                results = core.preflight(good, None)
                self.assertEqual(len(results), 5)
                self.assertEqual(core.preflight_failures(results), [])

                bad = self._cfg(db_mode=core.DB_MODE_NONE, port=str(free),
                                project_path=tmp, room_name="ghost")
                core.otree_available = lambda: False
                fails = core.preflight_failures(core.preflight(bad, None))
                self.assertEqual(sorted(f["check"] for f in fails),
                                 ["otree", "project"])
            finally:
                core.otree_available = real


# ---------------------------------------------------------------------------
# Round 3: database registry + researcher roster + open_dashboard helper
# ---------------------------------------------------------------------------
class TestDatabaseRegistry(unittest.TestCase):
    def test_builtins_always_present_and_first(self):
        ids = [d["id"] for d in core.list_databases({})]
        self.assertEqual(ids[:2], [core.DB_BUILTIN_SQLITE, core.DB_BUILTIN_LAB])
        for entry in core.builtin_databases():
            self.assertTrue(entry["builtin"])
        modes = {d["id"]: d["db_mode"] for d in core.builtin_databases()}
        self.assertEqual(modes[core.DB_BUILTIN_SQLITE], core.DB_MODE_NONE)
        self.assertEqual(modes[core.DB_BUILTIN_LAB], core.DB_MODE_LAB)

    def test_register_appends_and_is_global(self):
        extra = {}
        entry = core.register_database(
            extra, "Pilot DB", "A. Researcher",
            connection={"db_name": "pilot", "db_user": "pilot_rw", "db_password": "s",
                        "db_host": "h", "db_port": "5432"})
        self.assertEqual(entry["title"], "Pilot DB")
        self.assertEqual(entry["researcher"], "A. Researcher")
        # The Postgres user is kept DISTINCT from the researcher (person).
        self.assertEqual(entry["postgres_user"], "pilot_rw")
        self.assertEqual(entry["db_mode"], core.DB_MODE_CUSTOM)
        # It shows up in the global picker for ANY config (no per-config scoping).
        ids = [d["id"] for d in core.list_databases(extra)]
        self.assertIn(entry["id"], ids)
        self.assertEqual(len(core.known_databases_from_store(extra)), 1)

    def test_ids_are_unique_and_never_shadow_builtins(self):
        extra = {}
        a = core.register_database(extra, "Shared", "X", connection={"db_name": "a"})
        b = core.register_database(extra, "Shared", "Y", connection={"db_name": "b"})
        self.assertNotEqual(a["id"], b["id"])
        clash = core.register_database(extra, "oTree default", "Z",
                                       connection={"db_name": "c"})
        self.assertNotIn(clash["id"], (core.DB_BUILTIN_SQLITE, core.DB_BUILTIN_LAB))

    def test_append_only_persists_through_store(self):
        extra = {}
        core.register_database(extra, "Keep", "R", connection={"db_name": "keep"})
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "presets.json")
            core.save_store([core.default_preset()], extra, path)
            _presets, loaded = core.load_store(path)
        self.assertEqual([d["title"] for d in core.known_databases_from_store(loaded)],
                         ["Keep"])

    def test_config_fields_by_kind(self):
        self.assertEqual(
            core.database_config_fields({"db_mode": core.DB_MODE_NONE}),
            {"db_mode": core.DB_MODE_NONE})
        lab = core.database_config_fields({"db_mode": core.DB_MODE_LAB})
        self.assertEqual(lab["db_mode"], core.DB_MODE_LAB)
        self.assertIn("db_name", lab)
        custom = core.database_config_fields(
            {"db_mode": core.DB_MODE_CUSTOM, "db_name": "c", "db_user": "u",
             "db_password": "p", "db_host": "h", "db_port": "1"})
        self.assertEqual(custom["db_name"], "c")
        self.assertEqual(custom["db_user"], "u")

    def test_find_database(self):
        extra = {}
        e = core.register_database(extra, "Findable", "R", connection={"db_name": "f"})
        self.assertEqual(core.find_database(extra, e["id"])["title"], "Findable")
        self.assertEqual(core.find_database(extra, core.DB_BUILTIN_LAB)["db_mode"],
                         core.DB_MODE_LAB)
        self.assertIsNone(core.find_database(extra, "nope"))


class TestDefaultDatabaseReference(unittest.TestCase):
    """The lab-shared DEFAULT database is a REFERENCE into the one list.

    Proves the exact bug fix: a custom database created AFTER the setup wizard can
    be promoted to the lab-shared default, and DB_MODE_LAB then resolves to it.
    Each test writes a throwaway lab_info.json (so the wizard credentials are
    known) and restores the module state afterwards.
    """

    def setUp(self):
        self._saved = (core.LAB_INFO, dict(core.LAB_DB), dict(core.DEFAULT_CONFIG))
        self._tmp = tempfile.TemporaryDirectory()
        self._info_path = os.path.join(self._tmp.name, "lab_info.json")
        import json
        with open(self._info_path, "w", encoding="utf-8") as handle:
            json.dump({
                "default_lab": "lab", "labs": {},
                "database": {"db_name": "wizard_db", "db_user": "wiz_u",
                             "db_password": "wiz_pw", "db_host": "wiz.host",
                             "db_port": "5432"},
                "admin": {"username": "admin", "password": "a"},
            }, handle)
        core.reload_lab_info(self._info_path)

    def tearDown(self):
        core.LAB_INFO, lab_db, default_cfg = self._saved
        core.LAB_DB = lab_db
        core.DEFAULT_CONFIG.update(default_cfg)
        self._tmp.cleanup()

    def _lab_url(self):
        cfg = dict(core.DEFAULT_CONFIG, db_mode=core.DB_MODE_LAB)
        return core.build_database_url(cfg)

    def test_unset_reference_seeds_the_wizard_database_as_default(self):
        extra = {}
        core.apply_default_database(extra)
        self.assertEqual(core.default_database_id(extra), core.DB_BUILTIN_LAB)
        self.assertEqual(self._lab_url(),
                         "postgres://wiz_u:wiz_pw@wiz.host:5432/wizard_db")

    def test_post_wizard_custom_can_be_promoted_to_default(self):
        extra = {}
        core.apply_default_database(extra)
        a = core.register_database(extra, "Alpha", "R1", connection={
            "db_name": "alpha", "db_user": "ua", "db_password": "pa",
            "db_host": "ha", "db_port": "5001"})
        b = core.register_database(extra, "Beta", "R2", connection={
            "db_name": "beta", "db_user": "ub", "db_password": "pb",
            "db_host": "hb", "db_port": "5002"})
        # A custom created AFTER the wizard is offered as a default option...
        opt_ids = [e["id"] for e in core.default_database_options(extra)]
        self.assertIn(a["id"], opt_ids)
        self.assertIn(b["id"], opt_ids)
        self.assertNotIn(core.DB_BUILTIN_SQLITE, opt_ids)
        # ...and promoting it redirects DB_MODE_LAB to its URL (the bug fix).
        core.set_default_database(extra, a["id"])
        self.assertEqual(self._lab_url(), "postgres://ua:pa@ha:5001/alpha")
        # Switching to the second custom re-points it again.
        core.set_default_database(extra, b["id"])
        self.assertEqual(self._lab_url(), "postgres://ub:pb@hb:5002/beta")
        # Switching back to the lab built-in restores the wizard credentials.
        core.set_default_database(extra, core.DB_BUILTIN_LAB)
        self.assertEqual(self._lab_url(),
                         "postgres://wiz_u:wiz_pw@wiz.host:5432/wizard_db")

    def test_reference_persists_through_the_store(self):
        extra = {}
        core.apply_default_database(extra)
        a = core.register_database(extra, "Alpha", "R1", connection={
            "db_name": "alpha", "db_user": "ua", "db_password": "pa",
            "db_host": "ha", "db_port": "5001"})
        core.set_default_database(extra, a["id"])
        path = os.path.join(self._tmp.name, "presets.json")
        core.save_store([core.default_preset()], extra, path)
        _presets, loaded = core.load_store(path)
        self.assertEqual(core.default_database_id(loaded), a["id"])
        core.apply_default_database(loaded)
        self.assertEqual(self._lab_url(), "postgres://ua:pa@ha:5001/alpha")

    def test_sqlite_cannot_be_the_lab_shared_default(self):
        extra = {}
        with self.assertRaises(ValueError):
            core.set_default_database(extra, core.DB_BUILTIN_SQLITE)
        with self.assertRaises(ValueError):
            core.set_default_database(extra, "no_such_db")

    def test_removed_custom_default_falls_back_to_the_lab_builtin(self):
        # A reference to a custom that is no longer in the registry resolves back
        # to the lab built-in rather than breaking.
        extra = {"databases": [], core.DEFAULT_DATABASE_KEY: "vanished_custom"}
        core.apply_default_database(extra)
        self.assertEqual(self._lab_url(),
                         "postgres://wiz_u:wiz_pw@wiz.host:5432/wizard_db")


class TestResearcherRoster(unittest.TestCase):
    def test_add_and_dedupe_case_insensitive(self):
        extra = {}
        core.add_researcher(extra, "J. Tait")
        core.add_researcher(extra, "j. tait")   # same person, different case
        core.add_researcher(extra, "  ")         # blank ignored
        self.assertEqual(core.researchers_from_store(extra), ["J. Tait"])

    def test_roster_merges_three_sources_sorted(self):
        extra = {}
        core.add_researcher(extra, "S. Vermeer")
        core.register_database(extra, "DB", "A. Researcher", connection={"db_name": "d"})
        presets = [{"author": "J. Tait"}, {"author": "builtin"}, {"author": ""}]
        roster = core.list_researchers(extra, presets)
        self.assertEqual(roster, ["A. Researcher", "J. Tait", "S. Vermeer"])
        self.assertNotIn("builtin", roster)

    def test_register_records_creator_in_roster(self):
        extra = {}
        core.register_database(extra, "DB", "New Person", connection={"db_name": "d"})
        self.assertIn("New Person", core.researchers_from_store(extra))


class TestOpenDashboard(unittest.TestCase):
    def test_url_has_credentials(self):
        self.assertTrue(core.url_has_credentials("http://u:p@host:8000/x"))
        self.assertTrue(core.url_has_credentials("https://admin:pw@1.2.3.4/room/study"))
        self.assertFalse(core.url_has_credentials("http://host:8000/x"))
        self.assertFalse(core.url_has_credentials(""))

    def test_plain_url_uses_default_browser(self):
        calls = {}
        real_find = core.find_preferred_browser
        real_wb = core.webbrowser.open
        try:
            core.find_preferred_browser = lambda: ("Chrome", lambda u: calls.setdefault("pref", u))
            core.webbrowser.open = lambda u: calls.setdefault("default", u) or True
            result = core.open_dashboard("http://host:8000/x")
        finally:
            core.find_preferred_browser = real_find
            core.webbrowser.open = real_wb
        self.assertFalse(result["credentialed"])
        self.assertFalse(result["used_preferred"])
        self.assertEqual(result["browser"], "default")
        self.assertNotIn("pref", calls)
        self.assertEqual(calls.get("default"), "http://host:8000/x")

    def test_credentialed_url_prefers_known_browser(self):
        calls = {}
        real_find = core.find_preferred_browser
        try:
            core.find_preferred_browser = lambda: ("Chrome", lambda u: calls.setdefault("pref", u))
            result = core.open_dashboard("http://u:p@host:8000/x")
        finally:
            core.find_preferred_browser = real_find
        self.assertTrue(result["credentialed"])
        self.assertTrue(result["used_preferred"])
        self.assertEqual(result["browser"], "Chrome")
        self.assertEqual(calls.get("pref"), "http://u:p@host:8000/x")

    def test_credentialed_url_falls_back_when_no_preferred(self):
        calls = {}
        real_find = core.find_preferred_browser
        real_wb = core.webbrowser.open
        try:
            core.find_preferred_browser = lambda: None
            core.webbrowser.open = lambda u: calls.setdefault("default", u) or True
            result = core.open_dashboard("http://u:p@host:8000/x")
        finally:
            core.find_preferred_browser = real_find
            core.webbrowser.open = real_wb
        self.assertTrue(result["credentialed"])
        self.assertFalse(result["used_preferred"])
        self.assertEqual(result["browser"], "default")
        self.assertEqual(calls.get("default"), "http://u:p@host:8000/x")


class TestReadinessPoll(unittest.TestCase):
    """The launch path opens the dashboard the instant the server responds:
    a readiness poll (wait_for_server) replaces the old blind fixed sleep."""

    def test_wait_for_server_returns_fast_once_a_server_responds(self):
        import http.server
        import threading
        import time

        class _Quiet(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), _Quiet)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            start = time.monotonic()
            ready = core.wait_for_server("127.0.0.1", port, timeout=5.0,
                                         interval=0.05)
            elapsed = time.monotonic() - start
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertTrue(ready)
        # A live server answers on the first probe: nowhere near the old 2s wait.
        self.assertLess(elapsed, 1.0)

    def test_wait_for_server_gives_up_after_timeout_without_raising(self):
        import time
        # A port nothing is listening on: must return False (never raise) and
        # respect the timeout instead of hanging.
        start = time.monotonic()
        ready = core.wait_for_server("127.0.0.1", 1, timeout=0.4, interval=0.05)
        elapsed = time.monotonic() - start
        self.assertFalse(ready)
        self.assertLess(elapsed, 3.0)

    def test_open_dashboard_polls_first_and_imposes_no_fixed_delay(self):
        import time
        order = []

        def fake_wait(host, port, timeout=8.0, interval=0.25):
            order.append("wait")
            return True

        def fake_login(host, port, user, pw):
            order.append("login")
            return "COOKIE"

        def fake_relay(cookie, url, host=None):
            return "http://127.0.0.1:0/"

        def fake_open(url):
            order.append("open")
            return True

        start = time.monotonic()
        result = core.open_dashboard_authenticated(
            "localhost", "8000", "study", "admin", "pw",
            auto_login=True, wait=fake_wait, login=fake_login,
            start_relay=fake_relay, open_url=fake_open)
        elapsed = time.monotonic() - start

        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "cookie")
        # The readiness poll runs BEFORE the login attempt, and there is no blind
        # sleep anywhere: a fake that returns instantly means near-zero time.
        self.assertEqual(order[0], "wait")
        self.assertIn("login", order)
        self.assertLess(elapsed, 0.5)

    def test_manual_path_also_polls_first(self):
        # auto_login off: still gates on readiness, no pre-open sleep.
        order = []
        core.open_dashboard_authenticated(
            "localhost", "8000", "study", "admin", "pw",
            auto_login=False,
            wait=lambda *a, **k: order.append("wait") or True,
            open_url=lambda u: order.append("open") or True)
        self.assertEqual(order, ["wait", "open"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
