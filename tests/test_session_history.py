#!/usr/bin/env python3
"""Tests for the session-history log and version + update-check (fable review
F + I). All in otree_core -- stdlib only, no tkinter, no network (the update
check is exercised with an injected fetcher and never touches GitHub).

Run:  pytest tests/test_session_history.py
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
import otree_core as core  # noqa: E402


def _cfg(**over):
    c = dict(core.DEFAULT_CONFIG)
    c["project_path"] = "/tmp/proj"
    c["lab"] = "small"
    c["room_name"] = "study"
    c.update(over)
    return core.normalize_config(c)


class TestSessionHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "sessions.jsonl")

    def test_launch_appends_a_well_formed_line(self):
        entry = core.record_session(
            _cfg(), config_name="Dictator", author="ada", outcome="ok",
            server_ready_seconds=3.14, path=self.path)
        self.assertIsNotNone(entry)
        # One physical line was written, and it is valid JSON with every field.
        with open(self.path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        self.assertEqual(len(lines), 1)
        obj = json.loads(lines[0])
        for key in ("timestamp", "config", "author", "project", "lab", "room",
                    "database", "seats", "resetdb", "outcome",
                    "server_ready_seconds"):
            self.assertIn(key, obj)
        self.assertEqual(obj["config"], "Dictator")
        self.assertEqual(obj["author"], "ada")
        self.assertEqual(obj["lab"], "small")
        self.assertEqual(obj["room"], "study")
        self.assertEqual(obj["outcome"], "ok")
        self.assertEqual(obj["server_ready_seconds"], 3.1)
        self.assertIsInstance(obj["seats"], int)
        self.assertIsInstance(obj["resetdb"], bool)

    def test_database_label_carries_no_secret(self):
        entry = core.record_session(
            _cfg(db_mode=core.DB_MODE_LAB, db_password="s3cret"),
            config_name="x", path=self.path)
        self.assertNotIn("s3cret", json.dumps(entry))
        self.assertIn("Postgres", entry["database"])
        none = core.build_session_entry(_cfg(db_mode=core.DB_MODE_NONE))
        self.assertIn("SQLite", none["database"])

    def test_outcome_is_normalised(self):
        self.assertEqual(core.build_session_entry(_cfg(), outcome="ok")["outcome"], "ok")
        self.assertEqual(core.build_session_entry(_cfg(), outcome="fail")["outcome"], "fail")
        self.assertEqual(core.build_session_entry(_cfg(), outcome="anything")["outcome"], "fail")

    def test_reader_returns_newest_first(self):
        # Write three with explicit timestamps so order is deterministic.
        for name, ts in (("first", "2026-09-20T09:00:00"),
                         ("second", "2026-09-21T09:00:00"),
                         ("third", "2026-09-22T09:00:00")):
            entry = core.build_session_entry(_cfg(), config_name=name, when=ts)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")
        got = core.read_sessions(path=self.path)
        self.assertEqual([e["config"] for e in got], ["third", "second", "first"])

    def test_reader_respects_limit_and_skips_bad_lines(self):
        for i in range(5):
            core.record_session(_cfg(), config_name="c%d" % i, path=self.path)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write("not json at all\n\n")
        got = core.read_sessions(limit=2, path=self.path)
        self.assertEqual(len(got), 2)
        # Missing file => [] (fail-soft, read-only).
        self.assertEqual(core.read_sessions(path=os.path.join(self.tmp, "nope.jsonl")), [])

    def test_record_is_fail_soft(self):
        # A path that cannot be created returns None rather than raising.
        bad = os.path.join(self.path, "cannot", "be", "a", "dir", "sessions.jsonl")
        # self.path is a file, so treating it as a directory parent must fail
        with open(self.path, "w") as handle:
            handle.write("")
        self.assertIsNone(core.record_session(_cfg(), path=bad))


class TestVersionAndUpdateCheck(unittest.TestCase):
    # A GitHub releases/latest response has a tag_name (e.g. "v1.2.0").
    def _release(self, tag):
        return json.dumps({"tag_name": tag, "name": tag, "html_url": core.REPO_URL})

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cache = os.path.join(self.tmp, "update_check.json")

    def test_version_string_is_present_and_is_semver(self):
        self.assertTrue(core.APP_VERSION)
        self.assertEqual(core.APP_VERSION, "1.0.0")
        # Parses cleanly as MAJOR.MINOR.PATCH.
        self.assertEqual(core.parse_semver(core.APP_VERSION), (1, 0, 0))

    def test_semver_compare(self):
        self.assertEqual(core.parse_semver("v1.2.0"), (1, 2, 0))
        self.assertEqual(core.parse_semver("1.2"), (1, 2, 0))   # padded
        self.assertEqual(core.parse_semver("V2.0.1"), (2, 0, 1))
        self.assertIsNone(core.parse_semver("not-a-version"))
        self.assertIsNone(core.parse_semver(""))
        # strictly greater only
        self.assertTrue(core.version_is_newer("v1.0.1", "1.0.0"))
        self.assertTrue(core.version_is_newer("v2.0.0", "1.9.9"))
        self.assertTrue(core.version_is_newer("1.1.0", "1.0.9"))
        self.assertFalse(core.version_is_newer("1.0.0", "1.0.0"))   # equal is not newer
        self.assertFalse(core.version_is_newer("0.9.0", "1.0.0"))   # older
        self.assertFalse(core.version_is_newer("garbage", "1.0.0"))  # unparseable => not newer

    def test_parses_a_sample_github_release_response(self):
        self.assertEqual(core.parse_github_release_tag(self._release("v1.2.0")), "v1.2.0")
        # bytes and a list shape both work
        self.assertEqual(core.parse_github_release_tag(self._release("v1.2.0").encode()), "v1.2.0")
        as_list = json.dumps([{"tag_name": "v1.3.0"}])
        self.assertEqual(core.parse_github_release_tag(as_list), "v1.3.0")
        # garbage / no releases => None, never a phantom version
        self.assertIsNone(core.parse_github_release_tag("{}"))
        self.assertIsNone(core.parse_github_release_tag("not json"))
        self.assertIsNone(core.parse_github_release_tag(json.dumps({"message": "Not Found"})))

    def test_update_available_when_release_is_newer(self):
        result = core.check_for_update(
            fetcher=lambda: self._release("v1.2.0"), path=self.cache)
        self.assertTrue(result["update_available"])
        self.assertEqual(result["remote_version"], "v1.2.0")
        self.assertEqual(result["current"], core.APP_VERSION)
        self.assertTrue(result["checked"])
        self.assertEqual(result["label"], core.UPDATE_LABEL)
        self.assertEqual(result["tooltip"], core.UPDATE_TOOLTIP)
        self.assertEqual(result["repo_url"], core.REPO_URL)
        # It cached the check, so a second call is fresh and hits NO network.
        def boom():
            raise AssertionError("should not fetch when the cache is fresh")
        again = core.check_for_update(fetcher=boom, path=self.cache)
        self.assertFalse(again["checked"])
        self.assertTrue(again["update_available"])

    def test_no_update_when_release_is_not_newer(self):
        result = core.check_for_update(
            fetcher=lambda: self._release("v1.0.0"), path=self.cache)
        self.assertFalse(result["update_available"])
        self.assertEqual(result["label"], "")

    def test_fail_soft_on_network_error(self):
        def boom():
            raise OSError("no network")
        result = core.check_for_update(fetcher=boom, path=self.cache)
        # Silent: no crash, no update claimed, nothing cached.
        self.assertFalse(result["update_available"])
        self.assertFalse(result["checked"])
        self.assertEqual(result["label"], "")
        self.assertFalse(os.path.exists(self.cache))

    def test_fail_soft_on_no_releases_404(self):
        # GitHub returns 404 for a repo with no releases; the fetcher raising
        # HTTPError-like is swallowed => silent, no nudge.
        import urllib.error
        def not_found():
            raise urllib.error.HTTPError(core.GITHUB_RELEASES_URL, 404, "Not Found", {}, None)
        result = core.check_for_update(fetcher=not_found, path=self.cache)
        self.assertFalse(result["update_available"])
        self.assertFalse(result["checked"])

    def test_forced_check_bypasses_fresh_cache(self):
        core.check_for_update(fetcher=lambda: self._release("v1.2.0"), path=self.cache)
        forced = core.check_for_update(
            force=True, fetcher=lambda: self._release("v2.5.0"), path=self.cache)
        self.assertTrue(forced["checked"])
        self.assertEqual(forced["remote_version"], "v2.5.0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
