"""The HTTP fallback console: who may open one, and what it refuses.

The console itself needs Incus, which the machine running these tests does not
have - so the interesting part here is everything around it: ownership, state,
the CSRF token, and a stream token that no longer exists.
"""

from __future__ import annotations

import os
import unittest
import uuid
from pathlib import Path

os.environ.setdefault("ORION_DATA_DIR", "/tmp/orion-test-data")
os.environ.setdefault("ORION_SECRET_KEY", "testkey")

from fastapi.testclient import TestClient  # noqa: E402

from orion import db, security, util  # noqa: E402
from orion.app import app  # noqa: E402


class FallbackConsole(unittest.TestCase):
    def setUp(self):
        db.init()
        tag = uuid.uuid4().hex[:8]
        self.uid = db.execute(
            "INSERT INTO users(email, password_hash, role, created_at) VALUES(?, ?, 'client', ?)",
            ("{}@console.test".format(tag), "x", util.now()))
        self.other = db.execute(
            "INSERT INTO users(email, password_hash, role, created_at) VALUES(?, ?, 'client', ?)",
            ("{}@other.test".format(tag), "x", util.now()))
        self.mine = db.execute(
            "INSERT INTO instances(user_id, name, slug, incus_name, template, status, ssh_user, "
            "created_at) VALUES(?, ?, ?, ?, 'bare', 'running', 'x', ?)",
            (self.uid, tag, tag, "orion-{}-{}".format(tag, tag), util.now()))
        self.theirs = db.execute(
            "INSERT INTO instances(user_id, name, slug, incus_name, template, status, ssh_user, "
            "created_at) VALUES(?, ?, ?, ?, 'bare', 'running', 'x', ?)",
            (self.other, tag + "b", tag + "b", "orion-{}-{}b".format(tag, tag), util.now()))
        self.stopped = db.execute(
            "INSERT INTO instances(user_id, name, slug, incus_name, template, status, ssh_user, "
            "created_at) VALUES(?, ?, ?, ?, 'bare', 'stopped', 'x', ?)",
            (self.uid, tag + "c", tag + "c", "orion-{}-{}c".format(tag, tag), util.now()))
        self.session = security.create_session(self.uid, "127.0.0.1", "test-fallback")
        self.http = TestClient(app)
        self.http.cookies.set(security.SESSION_COOKIE, self.session["token"])

    def tearDown(self):
        for iid in (self.mine, self.theirs, self.stopped):
            db.execute("DELETE FROM instances WHERE id = ?", (iid,))
        for uid in (self.uid, self.other):
            db.execute("DELETE FROM users WHERE id = ?", (uid,))

    def test_someone_elses_instance_is_not_yours(self):
        r = self.http.get("/app/instances/{}/console/stream".format(self.theirs))

        self.assertEqual(r.status_code, 404)

    def test_a_stopped_instance_has_no_console(self):
        r = self.http.get("/app/instances/{}/console/stream".format(self.stopped))

        self.assertEqual(r.status_code, 409)
        self.assertIn("démarrée", r.text)

    def test_a_running_instance_is_accepted_but_reports_a_missing_hypervisor(self):
        """Where Incus is absent, the failure is explained rather than silent."""
        r = self.http.get("/app/instances/{}/console/stream".format(self.mine))

        self.assertEqual(r.status_code, 409)
        self.assertIn("console", r.text.lower())

    def test_input_needs_the_csrf_token(self):
        r = self.http.post("/app/instances/{}/console/input".format(self.mine),
                           headers={"x-console-token": "peu-importe"}, content=b"ls\r")

        self.assertEqual(r.status_code, 400, "un envoi sans jeton CSRF doit être refusé")

    def test_input_for_an_unknown_stream_is_refused(self):
        r = self.http.post("/app/instances/{}/console/input".format(self.mine),
                           headers={"x-console-token": "jeton-inconnu",
                                    "x-csrf-token": self.session["csrf"]},
                           content=b"ls\r")

        self.assertEqual(r.status_code, 409, "un flux disparu doit être signalé, pas ignoré")

    def test_the_routes_are_reachable_without_a_websocket_at_all(self):
        """The whole point: two ordinary requests, no upgrade anywhere."""
        body = (Path(__file__).resolve().parents[1] / "orion/routers/terminal.py").read_text()

        self.assertIn('@router.get("/app/instances/{instance_id}/console/stream"', body)
        self.assertIn('@router.post("/app/instances/{instance_id}/console/input"', body)
        self.assertIn("text/event-stream", body)


if __name__ == "__main__":
    unittest.main()
