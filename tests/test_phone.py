"""Phone confirmation: what it accepts, what it refuses, and what it limits.

The code never reaches the database in clear, so these capture it at the one place
it does exist - the send step - the same way a provider would receive it.
"""

from __future__ import annotations

import os
import unittest
import uuid

os.environ.setdefault("ORION_DATA_DIR", "/tmp/orion-test-data")
os.environ.setdefault("ORION_SECRET_KEY", "testkey")

from orion import db, phone, util  # noqa: E402


class Normalisation(unittest.TestCase):
    def test_a_french_mobile_is_written_out(self):
        self.assertEqual(phone.normalise("06 65 42 39 89"), "+33665423989")
        self.assertEqual(phone.normalise("06.65.42.39.89"), "+33665423989")
        self.assertEqual(phone.normalise("0033665423989"), "+33665423989")

    def test_a_number_already_in_international_form_is_kept(self):
        self.assertEqual(phone.normalise("+33 6 65 42 39 89"), "+33665423989")
        self.assertEqual(phone.normalise("+15550100"), "+15550100")

    def test_words_and_stumps_are_refused(self):
        for junk in ("", "azerty", "12345", "+", "06", "0" * 20):
            self.assertEqual(phone.normalise(junk), "", junk)

    def test_the_display_form_is_readable(self):
        self.assertEqual(phone.pretty("+33665423989"), "+33 6 65 42 39 89")


class Codes(unittest.TestCase):
    def setUp(self):
        db.init()
        self.tag = uuid.uuid4().hex[:10]
        self.uid = db.execute(
            "INSERT INTO users(email, password_hash, role, created_at) VALUES(?, ?, 'client', ?)",
            ("{}@phone.test".format(self.tag), "x", util.now()))
        self.number = "+33665423989"
        self.codes = []
        self._real_send = phone.send
        phone.send = self._capture

    def _capture(self, user_id, number, code):
        self.codes.append(code)
        return True

    def tearDown(self):
        phone.send = self._real_send
        db.execute("DELETE FROM phone_codes WHERE user_id = ?", (self.uid,))
        db.execute("DELETE FROM users WHERE id = ?", (self.uid,))

    def _issue(self):
        ok, message = phone.start(self.uid, self.number)
        self.assertTrue(ok, message)
        return self.codes[-1]

    def test_a_new_account_is_not_confirmed(self):
        self.assertFalse(phone.confirmed(db.one("SELECT * FROM users WHERE id = ?", (self.uid,))))

    def test_the_code_confirms_the_account(self):
        code = self._issue()

        ok, _ = phone.verify(self.uid, code)

        self.assertTrue(ok)
        self.assertTrue(phone.confirmed(db.one("SELECT * FROM users WHERE id = ?", (self.uid,))))

    def test_the_code_is_not_stored_in_clear(self):
        code = self._issue()

        rows = db.query("SELECT code_hash FROM phone_codes WHERE user_id = ?", (self.uid,))

        self.assertEqual(len(rows), 1)
        self.assertNotIn(code, rows[0]["code_hash"])

    def test_a_wrong_code_is_refused_and_counted(self):
        self._issue()

        ok, _ = phone.verify(self.uid, "000000")

        self.assertFalse(ok)
        self.assertEqual(db.one("SELECT attempts FROM phone_codes WHERE user_id = ?",
                                (self.uid,))["attempts"], 1)

    def test_five_wrong_codes_exhaust_it_even_for_the_right_one(self):
        code = self._issue()
        for _ in range(phone.MAX_ATTEMPTS):
            phone.verify(self.uid, "000000")

        ok, message = phone.verify(self.uid, code)

        self.assertFalse(ok, "un code devinable ne confirme rien")
        self.assertIn("tentatives", message)

    def test_an_expired_code_is_refused(self):
        code = self._issue()
        db.execute("UPDATE phone_codes SET expires_at = datetime('now', '-1 hour') WHERE user_id = ?",
                   (self.uid,))

        ok, message = phone.verify(self.uid, code)

        self.assertFalse(ok)
        self.assertIn("expir", message)

    def test_a_code_is_used_once(self):
        code = self._issue()
        phone.verify(self.uid, code)

        ok, _ = phone.verify(self.uid, code)

        self.assertFalse(ok, "le code doit être consommé")

    def test_sending_is_limited_per_account(self):
        for _ in range(phone.MAX_SENDS_PER_HOUR):
            self._issue()

        ok, message = phone.start(self.uid, self.number)

        self.assertFalse(ok, "un compte ne doit pas pouvoir arroser un numéro")
        self.assertIn("Trop de demandes", message)

    def test_a_refused_number_never_gets_a_code(self):
        ok, message = phone.start(self.uid, "")

        self.assertFalse(ok)
        self.assertEqual(len(self.codes), 0)


if __name__ == "__main__":
    unittest.main()
