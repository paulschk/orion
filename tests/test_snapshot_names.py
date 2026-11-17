"""Snapshot names a client reads, and that Incus accepts."""

import os
import unittest

os.environ.setdefault("ORION_DATA_DIR", "/tmp/orion-test-data")
os.environ.setdefault("ORION_SECRET_KEY", "testkey")

from orion.util import snapshot_label, slugify  # noqa: E402

STAMP = "2026-11-17-2217"


class LabelShape(unittest.TestCase):
    def test_ends_alphanumerically_whatever_the_input(self):
        # Incus refuses a name that does not end with a letter or a digit. A long
        # slug plus a label used to be truncated to a trailing dash and rejected
        # with a raw daemon error the client could do nothing with.
        for slug in ("dockertest", "wordpress",
                     "un-slug-vraiment-tres-tres-long-pour-un-client"):
            for requested in ("", "avant-maj", "essai-interface", "Test MAJ!", "x" * 40):
                name = snapshot_label(slug, requested, STAMP)
                self.assertTrue(name[-1].isalnum(), name)
                self.assertTrue(name[0].isalnum(), name)
                self.assertLessEqual(len(name), 63, name)

    def test_the_timestamp_survives_whatever_gets_shortened(self):
        name = snapshot_label("un-slug-vraiment-tres-long-pour-un-client", "avant-maj", STAMP)
        self.assertIn(STAMP, name)

    def test_the_human_part_stays_readable_when_there_is_room(self):
        self.assertEqual(snapshot_label("bea", "avant-maj", STAMP), f"bea-avant-maj-{STAMP}")

    def test_no_label_gives_instance_and_date(self):
        self.assertEqual(snapshot_label("bea", "", STAMP), f"bea-{STAMP}")

    def test_a_collision_suffix_stays_valid_and_ordered(self):
        first = snapshot_label("bea", "avant-maj", STAMP)
        second = snapshot_label("bea", "avant-maj", STAMP, suffix=2)
        self.assertNotEqual(first, second)
        self.assertTrue(second.endswith(f"{STAMP}-2"), second)


class SlugifyShape(unittest.TestCase):
    def test_never_returns_a_trailing_dash(self):
        # The regression itself: the truncation happened before the strip.
        for value in ("dockertest-essai-interface-2026-11-17-2217",
                      "a-very-long-name-that-gets-cut-exactly-",
                      "x" * 60):
            self.assertFalse(slugify(value).endswith("-"), value)
            self.assertTrue(slugify(value))


if __name__ == "__main__":
    unittest.main()
