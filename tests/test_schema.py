"""The is_primary backfill must be a one-shot, not a startup habit.

It re-promoted, on every boot, any route a client had under their own instance's
name - including one pointing at a port where nothing listens, which made the
dead address the instance's main one.

Isolation note: `orion.config.settings` is built once, at first import, so the
ORION_DATA_DIR that happened to be set then is the database every test module
uses. A test here therefore cannot assume a virgin database - it has to use
values unique to the run, or it passes on the first run and fails on the second.
"""

import os
import unittest
import uuid

os.environ.setdefault("ORION_DATA_DIR", "/tmp/orion-test-data")
os.environ.setdefault("ORION_SECRET_KEY", "testkey")

from orion import db, util  # noqa: E402


class OneShotBackfill(unittest.TestCase):
    def setUp(self):
        db.init()
        tag = uuid.uuid4().hex[:10]
        self.slug = "bk" + tag
        self.domain = f"{self.slug}.hosting.node29.fr"
        self.uid = db.execute(
            "INSERT INTO users(email, password_hash, role, created_at) "
            "VALUES(?, ?, 'client', ?)", (f"{tag}@backfill.test", "x", util.now()))
        self.iid = db.execute(
            "INSERT INTO instances(user_id, name, slug, incus_name, template, status, "
            "ssh_user, created_at) VALUES(?, ?, ?, ?, ?, 'running', 'x', ?)",
            (self.uid, self.slug, self.slug, f"orion-{tag}-{self.slug}", "docker", util.now()))
        # Exactly the instance's generated name, but not marked as its address.
        self.sid = db.execute(
            "INSERT INTO sites(instance_id, domain, upstream, enabled, is_primary, "
            "created_at) VALUES(?, ?, 80, 1, 0, ?)", (self.iid, self.domain, util.now()))

    def _primary(self):
        row = db.one("SELECT is_primary FROM sites WHERE id = ?", (self.sid,))
        return row["is_primary"] if row else None

    def test_a_route_left_unmarked_stays_unmarked_across_boots(self):
        for _ in range(3):
            db.init()

        self.assertEqual(self._primary(), 0,
                         "the backfill must not re-run on an existing schema")

    def test_the_backfill_still_runs_when_the_column_is_new(self):
        # Drop the column to simulate a database from before it existed.
        with db.cursor(commit=True) as cur:
            cur.execute("ALTER TABLE sites DROP COLUMN is_primary")
        db.init()                      # re-adds the column and backfills once

        self.assertEqual(self._primary(), 1, "the first run must still migrate")

    def tearDown(self):
        # Leave the shared database as it was found.
        db.execute("DELETE FROM sites WHERE instance_id = ?", (self.iid,))
        db.execute("DELETE FROM instances WHERE id = ?", (self.iid,))
        db.execute("DELETE FROM users WHERE id = ?", (self.uid,))


if __name__ == "__main__":
    unittest.main()
