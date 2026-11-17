"""The is_primary backfill must be a one-shot, not a startup habit.

It re-promoted, on every boot, any route a client had under their own instance's
name - including one pointing at a port where nothing listens, which made the
dead address the instance's main one.
"""

import os
import shutil
import tempfile
import unittest

DATA = tempfile.mkdtemp(prefix="orion-schema-")
os.environ["ORION_DATA_DIR"] = DATA
os.environ.setdefault("ORION_SECRET_KEY", "testkey")

from orion import db, util  # noqa: E402


class OneShotBackfill(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(DATA, ignore_errors=True)

    def test_a_marked_route_is_left_alone_on_the_next_boot(self):
        db.init()
        uid = db.execute("INSERT INTO users(email, password_hash, role, created_at) "
                         "VALUES(?, ?, 'client', ?)", ("a@b.test", "x", util.now()))
        iid = db.execute(
            "INSERT INTO instances(user_id, name, slug, incus_name, template, status, "
            "ssh_user, created_at) VALUES(?, ?, ?, ?, ?, 'running', 'x', ?)",
            (uid, "demo", "demo", "orion-1-demo", "docker", util.now()))
        # Exactly the instance's generated name, but not marked as its address.
        sid = db.execute("INSERT INTO sites(instance_id, domain, upstream, enabled, "
                         "is_primary, created_at) VALUES(?, ?, 80, 1, 0, ?)",
                         (iid, "demo.hosting.node29.fr", util.now()))

        db.init()          # a second boot

        row = db.one("SELECT is_primary FROM sites WHERE id = ?", (sid,))
        self.assertEqual(row["is_primary"], 0,
                         "the backfill must not re-run on an existing schema")

    def test_the_backfill_still_runs_when_the_column_is_new(self):
        db.init()
        row = db.one("SELECT is_primary FROM sites WHERE domain = ?",
                     ("demo.hosting.node29.fr",))
        # Drop the column to simulate a database from before it existed.
        with db.cursor(commit=True) as cur:
            cur.execute("ALTER TABLE sites DROP COLUMN is_primary")
        db.init()          # adds the column and backfills once
        row = db.one("SELECT is_primary FROM sites WHERE domain = ?",
                     ("demo.hosting.node29.fr",))
        self.assertEqual(row["is_primary"], 1, "the first run must still migrate")


if __name__ == "__main__":
    unittest.main()
