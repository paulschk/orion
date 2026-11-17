"""What a client may name their own address, and what is already spoken for."""

import os
import unittest
import uuid

os.environ.setdefault("ORION_DATA_DIR", "/tmp/orion-test-data")
os.environ.setdefault("ORION_SECRET_KEY", "testkey")

from orion import db, util  # noqa: E402
from orion.config import settings  # noqa: E402
from orion.routers.client import label_problem  # noqa: E402


class LabelRules(unittest.TestCase):
    def setUp(self):
        db.init()
        tag = uuid.uuid4().hex[:10]
        self.tag = tag
        self.own_slug = "own" + tag
        self.uid = db.execute(
            "INSERT INTO users(email, password_hash, role, created_at) "
            "VALUES(?, ?, 'client', ?)", (f"{tag}@labels.test", "x", util.now()))
        self.iid = db.execute(
            "INSERT INTO instances(user_id, name, slug, incus_name, template, status, "
            "ssh_user, created_at) VALUES(?, ?, ?, ?, 'lemp', 'running', 'x', ?)",
            (self.uid, self.own_slug, self.own_slug, f"orion-{tag}-own", util.now()))

    def tearDown(self):
        db.execute("DELETE FROM sites WHERE instance_id IN "
                   "(SELECT id FROM instances WHERE user_id = ?)", (self.uid,))
        db.execute("DELETE FROM instances WHERE user_id = ?", (self.uid,))
        db.execute("DELETE FROM users WHERE id = ?", (self.uid,))

    def _other_instance_with_site(self, label):
        iid = db.execute(
            "INSERT INTO instances(user_id, name, slug, incus_name, template, status, "
            "ssh_user, created_at) VALUES(?, ?, ?, ?, 'lemp', 'running', 'x', ?)",
            (self.uid, f"o{label}{self.tag}", f"o{label}{self.tag}",
             f"orion-{self.tag}-{label}", util.now()))
        db.execute("INSERT INTO sites(instance_id, domain, upstream, enabled, is_primary, "
                   "created_at) VALUES(?, ?, 80, 1, 0, ?)",
                   (iid, f"{label}.{settings.domain}", util.now()))
        return iid

    def test_a_plain_name_is_accepted(self):
        self.assertEqual(label_problem(f"demo-{self.tag}", instance_id=self.iid), "")

    def test_the_instances_own_slug_is_allowed(self):
        # Its generated address must not be refused for colliding with itself.
        self.assertEqual(label_problem(self.own_slug, instance_id=self.iid), "")

    def test_system_names_are_refused(self):
        for name in ("www", "ns1", "admin", "ssh", "hosting", "static"):
            self.assertIn("réservé", label_problem(name, instance_id=self.iid), name)

    def test_shape_is_enforced(self):
        for bad in ("", "-demo", "demo-", "Demo", "démo", "de mo", "a" * 64, "demo_x"):
            self.assertTrue(label_problem(bad, instance_id=self.iid), f"accepte a tort: {bad!r}")

    def test_an_address_of_another_instance_is_refused(self):
        label = f"taken{self.tag}"
        self._other_instance_with_site(label)
        problem = label_problem(label, instance_id=self.iid)
        self.assertIn("déjà utilisé", problem)
        # And it stays refused when this instance owns no site at all yet.
        self.assertIn("déjà utilisé", label_problem(label, instance_id=self.iid, site_id=None))

    def test_a_second_address_of_the_same_instance_is_refused(self):
        label = f"self{self.tag}"
        sid = db.execute(
            "INSERT INTO sites(instance_id, domain, upstream, enabled, is_primary, "
            "created_at) VALUES(?, ?, 80, 1, 0, ?)",
            (self.iid, f"{label}.{settings.domain}", util.now()))
        self.assertIn("déjà une adresse de cette instance",
                      label_problem(label, instance_id=self.iid, site_id=None))
        # Renaming that very site to its current name is a no-op, not a clash.
        self.assertEqual(label_problem(label, instance_id=self.iid, site_id=sid), "")

    def test_another_instances_slug_is_refused(self):
        # That instance will generate this address for itself when published.
        self.assertIn("nom d'une autre instance",
                      label_problem(self.own_slug, instance_id=self.iid + 999)  )


if __name__ == "__main__":
    unittest.main()
