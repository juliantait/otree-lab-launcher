#!/usr/bin/env python3
"""Unit tests for the DATABASE-management bundle (Round: DB management).

Covers, with no live Postgres needed:
  1. slugify_pg_dbname  -- the Create-database name prefill helper
  2. edit_database      -- editing a custom entry AND the lab-shared built-in
  3. register-without-create ("already exists" checkbox) parity
  4. issue_fix_for + switch_to_lab_default -- the "Use lab default instead"
     recovery on a DB connection failure

Run:  python3 _ai/test_db_management.py
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
import otree_core as core  # noqa: E402


class TestSlugifyPgDbname(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(core.slugify_pg_dbname("My Lab-Study 2"), "my_lab_study_2")
        self.assertEqual(core.slugify_pg_dbname("2024data"), "_2024data")

    def test_rules(self):
        cases = {
            "  spaces  here ": "spaces_here",
            "Weird!!Name??": "weird_name",
            "": "_",
            "already_ok": "already_ok",
            "UPPER": "upper",
            "a---b": "a_b",
            "cost$field": "cost$field",
            "___x___": "x",
            None: "_",
        }
        for src, want in cases.items():
            self.assertEqual(core.slugify_pg_dbname(src), want, "%r" % (src,))

    def test_result_is_a_valid_identifier(self):
        for src in ["My Lab-Study 2", "2024data", "Weird!!Name??", "", "9lives"]:
            slug = core.slugify_pg_dbname(src)
            ok, why = core.validate_pg_identifier(slug)
            self.assertTrue(ok, "%r -> %r not valid: %s" % (src, slug, why))


class _LabInfoTempMixin(unittest.TestCase):
    """Point OTREE_LAB_INFO at a throwaway lab_info.json for each test."""

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="dbmgmt-test-")
        self._path = os.path.join(self._dir, "lab_info.json")
        with open(self._path, "w") as handle:
            json.dump({
                "default_lab": "small",
                "labs": {"small": {"name": "Small", "host": "10.0.0.5",
                                   "seats": ["1", "2"], "default_room": "study"}},
                "database": {"db_name": "otree", "db_user": "otree",
                             "db_password": "labpw", "db_host": "labhost",
                             "db_port": "5432"},
                "admin": {"username": "admin", "password": "adminpw"},
            }, handle)
        self._old_env = os.environ.get("OTREE_LAB_INFO")
        os.environ["OTREE_LAB_INFO"] = self._path
        core.reload_lab_info()

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("OTREE_LAB_INFO", None)
        else:
            os.environ["OTREE_LAB_INFO"] = self._old_env
        core.reload_lab_info()


class TestEditDatabase(_LabInfoTempMixin):
    def _seed(self):
        extra = {}
        core.apply_default_database(extra)
        e1 = core.register_database(
            extra, title="Alpha DB", researcher="Alice",
            connection={"db_name": "alpha", "db_user": "alice", "db_password": "pw1",
                        "db_host": "h1", "db_port": "5432"})
        e2 = core.register_database(
            extra, title="Beta DB", researcher="Bob",
            connection={"db_name": "beta", "db_user": "bob", "db_password": "pw2",
                        "db_host": "h2", "db_port": "5433"})
        return extra, e1, e2

    def test_edit_custom_persists_and_url_reflects(self):
        extra, e1, e2 = self._seed()
        self.assertEqual(
            core.build_database_url(core.database_config_fields(e1)),
            "postgres://alice:pw1@h1:5432/alpha")
        edited = core.edit_database(extra, e1["id"], db_name="alpha2", db_user="alice2")
        self.assertEqual(edited["id"], e1["id"])
        self.assertEqual(edited["db_name"], "alpha2")
        self.assertEqual(edited["db_user"], "alice2")
        self.assertEqual(edited["postgres_user"], "alice2")
        found = core.find_database(extra, e1["id"])
        self.assertEqual(found["db_name"], "alpha2")
        self.assertEqual(
            core.build_database_url(core.database_config_fields(found)),
            "postgres://alice2:pw1@h1:5432/alpha2")
        # The other entry is untouched.
        self.assertEqual(core.find_database(extra, e2["id"])["db_name"], "beta")

    def test_edit_custom_title_and_researcher(self):
        extra, e1, _ = self._seed()
        edited = core.edit_database(extra, e1["id"], title="Renamed",
                                    researcher="Alice Smith")
        self.assertEqual(edited["title"], "Renamed")
        self.assertEqual(edited["researcher"], "Alice Smith")
        self.assertIn("Alice Smith", core.list_researchers(extra))

    def test_edit_lab_shared_writes_lab_info(self):
        extra = {}
        core.apply_default_database(extra)
        with open(self._path) as handle:
            before = json.load(handle)["database"]
        self.assertEqual(before["db_host"], "labhost")
        entry = core.edit_database(extra, core.DB_BUILTIN_LAB,
                                   db_host="newhost", db_password="np")
        with open(self._path) as handle:
            after = json.load(handle)["database"]
        self.assertEqual(after["db_host"], "newhost")
        self.assertEqual(after["db_password"], "np")
        self.assertEqual(after["db_name"], "otree")   # untouched field kept
        self.assertEqual(core.LAB_DB["db_host"], "newhost")
        self.assertEqual(entry["db_host"], "newhost")
        labcfg = core.normalize_config({"db_mode": core.DB_MODE_LAB})
        self.assertEqual(labcfg["db_host"], "newhost")

    def test_edit_sqlite_builtin_raises(self):
        extra = {}
        with self.assertRaises(ValueError):
            core.edit_database(extra, core.DB_BUILTIN_SQLITE, db_name="x")

    def test_edit_unknown_id_raises(self):
        extra = {}
        with self.assertRaises(ValueError):
            core.edit_database(extra, "nope", db_name="x")


class TestRegisterAlreadyExists(_LabInfoTempMixin):
    def test_register_without_create_is_selectable_and_editable(self):
        # The "already exists" path registers the connection WITHOUT create_database.
        extra = {}
        entry = core.register_database(
            extra, title="Existing", researcher="Carol",
            connection={"db_name": "existing", "db_user": "carol", "db_password": "p",
                        "db_host": "h3", "db_port": "5432"})
        self.assertIsNotNone(core.find_database(extra, entry["id"]))
        # Identical shape to a created-and-registered entry.
        created = core.register_database(
            extra, title="Created", researcher="Dave",
            connection={"db_name": "created", "db_user": "dave", "db_password": "p",
                        "db_host": "h4", "db_port": "5432"})
        self.assertEqual(set(entry.keys()), set(created.keys()))
        edited = core.edit_database(extra, entry["id"], db_host="h3b")
        self.assertEqual(edited["db_host"], "h3b")


class TestUseLabDefaultRecovery(_LabInfoTempMixin):
    def test_issue_fix_offered_for_custom_db_failure(self):
        failure = {"check": "database", "ok": False, "db_mode": core.DB_MODE_CUSTOM,
                   "message": "Could not connect to the chosen database: refused"}
        meta = core.issue_fix_for(failure)
        self.assertEqual(meta.get("fix"), "use_lab_default")
        self.assertTrue(meta.get("fix_label"))

    def test_issue_fix_not_offered_for_lab_db_failure(self):
        failure = {"check": "database", "ok": False, "db_mode": core.DB_MODE_LAB,
                   "message": "Could not connect to the lab database: refused"}
        self.assertNotEqual(core.issue_fix_for(failure).get("fix"), "use_lab_default")

    def test_switch_to_lab_default_flips_mode_and_resolves(self):
        extra = {}
        core.apply_default_database(extra)
        custom = core.normalize_config({
            "db_mode": core.DB_MODE_CUSTOM, "db_name": "alpha", "db_user": "alice",
            "db_password": "pw1", "db_host": "h1", "db_port": "5432"})
        switched = core.switch_to_lab_default(custom)
        self.assertEqual(switched["db_mode"], core.DB_MODE_LAB)
        self.assertEqual(switched["db_host"], core.LAB_DB["db_host"])
        self.assertEqual(
            core.build_database_url(switched),
            core.build_database_url(core.normalize_config({"db_mode": core.DB_MODE_LAB})))


if __name__ == "__main__":
    unittest.main(verbosity=2)
