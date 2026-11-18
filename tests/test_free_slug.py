"""An address may not reveal that other instances exist."""

import os
import re
import unittest
import uuid

os.environ.setdefault("ORION_DATA_DIR", "/tmp/orion-test-data")
os.environ.setdefault("ORION_SECRET_KEY", "testkey")

from orion import db  # noqa: E402
from orion.routers.client import free_slug  # noqa: E402
from orion.util import slugify  # noqa: E402


class FreeSlug(unittest.TestCase):
    def setUp(self):
        self.added = []
        self.tag = uuid.uuid4().hex[:8]

    def tearDown(self):
        for slug in self.added:
            db.execute("DELETE FROM instances WHERE slug = ?", (slug,))

    def _taken(self, slug, status="destroyed"):
        db.execute(
            "INSERT INTO instances(user_id, plan_id, name, slug, incus_name, machine_id, template, "
            "status, created_at, expires_at) VALUES(1, 1, ?, ?, ?, ?, 'bare', ?, "
            "'2026-01-01T00:00:00+00:00', '2027-01-01T00:00:00+00:00')",
            (slug, slug, f"orion-t-{slug}", uuid.uuid4().hex, status))
        self.added.append(slug)

    def test_a_free_name_is_kept_as_it_is(self):
        wanted = f"vitrine-{self.tag}"
        self.assertEqual(free_slug(wanted), wanted)
        self.assertEqual(free_slug(wanted), slugify(wanted))

    def test_a_taken_name_does_not_get_a_counter(self):
        # This is the whole point: "wordpress" then "wordpress-2" told every
        # visitor how many other instances existed.
        taken = f"wordpress-{self.tag}"
        self._taken(taken)
        got = free_slug(taken)
        self.assertNotEqual(got, taken)
        self.assertNotIn(self.tag, got, "l'adresse reprend le nom pris")
        self.assertIsNone(re.search(r"\d", got), f"l'adresse contient un chiffre : {got}")

    def test_the_random_address_is_two_words_and_dns_safe(self):
        taken = f"blog-{self.tag}"
        self._taken(taken)
        got = free_slug(taken)
        self.assertEqual(got, slugify(got))
        self.assertEqual(len(got.split("-")), 2, got)

    def test_a_destroyed_instance_still_holds_its_name(self):
        # The container is gone but the row is not, so the name is not free.
        taken = f"vieux-{self.tag}"
        self._taken(taken, status="destroyed")
        self.assertNotEqual(free_slug(taken), taken)


if __name__ == "__main__":
    unittest.main()
