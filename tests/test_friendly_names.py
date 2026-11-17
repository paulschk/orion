"""Generated instance names must not count the neighbours."""

import os
import unittest

os.environ.setdefault("ORION_DATA_DIR", "/tmp/orion-test-data")
os.environ.setdefault("ORION_SECRET_KEY", "testkey")

from orion.util import _FRIENDLY_ADJECTIVES, _FRIENDLY_NOUNS, friendly_name, slugify  # noqa: E402


class GeneratedNames(unittest.TestCase):
    def test_it_looks_like_two_words(self):
        name = friendly_name()
        parts = name.split("-")
        self.assertEqual(len(parts), 2, name)
        self.assertIn(parts[0], _FRIENDLY_ADJECTIVES)
        self.assertIn(parts[1], _FRIENDLY_NOUNS)

    def test_it_survives_slugify_unchanged(self):
        # The address is built from this, so anything slugify would rewrite is
        # a name the client reads differently from the address they get.
        for _ in range(200):
            name = friendly_name()
            self.assertEqual(slugify(name), name)

    def test_nothing_in_the_lists_carries_an_accent_or_a_dash(self):
        for word in _FRIENDLY_ADJECTIVES + _FRIENDLY_NOUNS:
            self.assertTrue(word.isascii(), word)
            self.assertNotIn("-", word)
            self.assertEqual(word, word.lower())

    def test_there_are_enough_combinations_to_look_arbitrary(self):
        self.assertGreaterEqual(len(_FRIENDLY_ADJECTIVES) * len(_FRIENDLY_NOUNS), 1000)

    def test_it_is_not_a_counter(self):
        seen = {friendly_name() for _ in range(60)}
        self.assertGreater(len(seen), 45, "trop peu de variete : les noms se repetent")


if __name__ == "__main__":
    unittest.main()
