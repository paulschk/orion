"""Sign-in throttling: the behaviour, not the wiring.

Proven live on the local mirror, where the seventh attempt with the *correct*
password answered 429; these keep it that way.
"""

from __future__ import annotations

import unittest

from orion.routers import auth


class LoginThrottleTests(unittest.TestCase):
    def setUp(self):
        auth._fails.clear()

    def tearDown(self):
        auth._fails.clear()

    def test_an_unknown_address_may_try(self):
        self.assertEqual(auth._retry_after("10.0.0.9"), 0)

    def test_the_door_closes_on_the_attempt_after_the_limit(self):
        for _ in range(auth._MAX_FAILS):
            self.assertEqual(auth._retry_after("10.0.0.1"), 0, "trop tôt")
            auth._note_failure("10.0.0.1")
        self.assertGreater(auth._retry_after("10.0.0.1"), 0)

    def test_the_counter_is_per_address(self):
        for _ in range(auth._MAX_FAILS):
            auth._note_failure("10.0.0.1")
        self.assertGreater(auth._retry_after("10.0.0.1"), 0)
        self.assertEqual(auth._retry_after("10.0.0.2"), 0, "voisin puni à tort")

    def test_an_address_that_never_identifies_itself_is_not_counted(self):
        auth._note_failure("")
        self.assertEqual(auth._retry_after(""), 0)

    def test_old_failures_fall_out_of_the_window(self):
        auth._fails["10.0.0.3"] = [0.0] * auth._MAX_FAILS      # epoch: long past
        self.assertEqual(auth._retry_after("10.0.0.3"), 0)
        self.assertNotIn("10.0.0.3", auth._fails, "l'entrée périmée devrait partir")

    def test_the_wait_is_never_announced_as_zero_minutes(self):
        for _ in range(auth._MAX_FAILS):
            auth._note_failure("10.0.0.4")
        self.assertGreaterEqual(auth._retry_after("10.0.0.4"), 1)


if __name__ == "__main__":
    unittest.main()
