"""Confirmation by email: what it accepts, what it refuses, what it limits.

The link is captured where it is really handed to a mail backend, so the token
under test is the one a client would receive.
"""

from __future__ import annotations

import os
import re
import unittest
import uuid

os.environ.setdefault("ORION_DATA_DIR", "/tmp/orion-test-data")
os.environ.setdefault("ORION_SECRET_KEY", "testkey")

from orion import db, mail, util  # noqa: E402


class Confirmation(unittest.TestCase):
    def setUp(self):
        db.init()
        tag = uuid.uuid4().hex[:10]
        self.uid = db.execute(
            "INSERT INTO users(email, password_hash, role, created_at) VALUES(?, ?, 'client', ?)",
            ("{}@mail.test".format(tag), "x", util.now()))
        self.user = db.one("SELECT * FROM users WHERE id = ?", (self.uid,))
        self.sent = []
        self._real_send = mail.send
        mail.send = self._capture

    def _capture(self, to, subject, body):
        self.sent.append({"to": to, "subject": subject, "body": body})
        return True

    def tearDown(self):
        mail.send = self._real_send
        db.execute("DELETE FROM email_tokens WHERE user_id = ?", (self.uid,))
        db.execute("DELETE FROM users WHERE id = ?", (self.uid,))

    def _token(self):
        self.sent.clear()
        ok, message = mail.issue(db.one("SELECT * FROM users WHERE id = ?", (self.uid,)))
        self.assertTrue(ok, message)
        self.assertEqual(len(self.sent), 1)
        found = re.search(r"token=([A-Za-z0-9_\-]+)", self.sent[0]["body"])
        self.assertIsNotNone(found, "le courriel doit porter un lien")
        return found.group(1)

    def test_a_new_account_is_not_confirmed(self):
        self.assertFalse(mail.confirmed(self.user))

    def test_the_link_confirms_the_account(self):
        token = self._token()

        ok, _, user = mail.confirm(token)

        self.assertTrue(ok)
        self.assertTrue(mail.confirmed(user))

    def test_the_token_is_not_stored_in_clear(self):
        token = self._token()

        rows = db.query("SELECT token_hash FROM email_tokens WHERE user_id = ?", (self.uid,))

        self.assertEqual(len(rows), 1)
        self.assertNotIn(token[:12], rows[0]["token_hash"])

    def test_a_link_works_once(self):
        token = self._token()
        mail.confirm(token)

        ok, message, _ = mail.confirm(token)

        self.assertFalse(ok, "un lien ne doit pas servir deux fois")
        self.assertIn("valable", message)

    def test_an_invented_token_is_refused(self):
        self._token()

        ok, _, _ = mail.confirm("x" * 43)

        self.assertFalse(ok)

    def test_a_short_stump_is_refused_without_touching_the_database(self):
        ok, message, _ = mail.confirm("court")

        self.assertFalse(ok)
        self.assertIn("invalide", message)

    def test_an_expired_link_is_refused(self):
        token = self._token()
        db.execute("UPDATE email_tokens SET expires_at = datetime('now', '-1 hour') "
                   "WHERE user_id = ?", (self.uid,))

        ok, message, _ = mail.confirm(token)

        self.assertFalse(ok)
        self.assertIn("valable", message)

    def test_sending_is_limited_per_account(self):
        for _ in range(mail.MAX_SENDS_PER_HOUR):
            self._token()

        ok, message = mail.issue(db.one("SELECT * FROM users WHERE id = ?", (self.uid,)))

        self.assertFalse(ok, "un compte ne doit pas pouvoir arroser une boîte")
        self.assertIn("Trop d'envois", message)

    def test_the_link_points_at_the_panel(self):
        self._token()

        self.assertIn("/register/confirmer?token=", self.sent[0]["body"])
        self.assertIn("48 heures", self.sent[0]["body"])


class Backends(unittest.TestCase):
    """The panel must be honest about whether it really sends anything."""

    def _with(self, name):
        real = mail.settings
        mail.settings = type("S", (), {"mail_backend": name, "mail_from": "a@b.c",
                                       "smtp_host": "", "smtp_port": 587,
                                       "smtp_user": "", "smtp_password": ""})()
        try:
            return mail.delivering()
        finally:
            mail.settings = real

    def test_log_does_not_pretend_to_deliver(self):
        self.assertFalse(self._with("log"))

    def test_sendmail_and_smtp_do_attempt_delivery(self):
        self.assertTrue(self._with("sendmail"))
        self.assertTrue(self._with("smtp"))


if __name__ == "__main__":
    unittest.main()
