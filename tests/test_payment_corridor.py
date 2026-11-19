"""The payment corridor: an order exists, nothing is built before it is paid.

The rule this pins down is the one the corridor was missing: a machine is never
provisioned on the strength of an order alone. Payment settles the invoice,
activates the subscription, and only then does the instance move on.
"""

from __future__ import annotations

import os
import unittest
import uuid
from pathlib import Path

os.environ.setdefault("ORION_DATA_DIR", "/tmp/orion-test-data")
os.environ.setdefault("ORION_SECRET_KEY", "testkey")

from orion import billing, db, util  # noqa: E402
from orion.routers import client  # noqa: E402


def _a_plan():
    plan = db.one("SELECT * FROM plans WHERE is_active = 1 ORDER BY sort_order")
    if plan is not None:
        return plan
    db.execute(
        "INSERT INTO plans(slug, name, vcpu, memory_mb, disk_gb, price_cents, period_days, "
        "sites, is_active, sort_order, description, features) "
        "VALUES('corridor-test', 'Corridor', 1, 512, 5, 300, 30, 1, 1, 99, '', '')")
    return db.one("SELECT * FROM plans WHERE slug = 'corridor-test'")


class PaymentCorridor(unittest.TestCase):
    def setUp(self):
        db.init()
        tag = uuid.uuid4().hex[:10]
        self.uid = db.execute(
            "INSERT INTO users(email, password_hash, role, created_at) VALUES(?, ?, 'client', ?)",
            ("{}@corridor.test".format(tag), "x", util.now()))
        self.plan = _a_plan()
        self.iid = db.execute(
            "INSERT INTO instances(user_id, name, slug, incus_name, template, status, "
            "ssh_user, created_at) VALUES(?, ?, ?, ?, 'bare', 'awaiting_payment', 'x', ?)",
            (self.uid, tag, tag, "orion-{}-{}".format(tag, tag), util.now()))
        self.sub, self.inv = billing.subscribe(self.uid, self.plan["id"], self.iid,
                                               status="pending")

    def tearDown(self):
        db.execute("DELETE FROM invoices WHERE id = ?", (self.inv,))
        db.execute("DELETE FROM subscriptions WHERE id = ?", (self.sub,))
        db.execute("DELETE FROM instances WHERE id = ?", (self.iid,))
        db.execute("DELETE FROM users WHERE id = ?", (self.uid,))

    def test_an_order_leaves_the_subscription_pending_and_the_invoice_due(self):
        self.assertEqual(db.one("SELECT status FROM subscriptions WHERE id = ?",
                                (self.sub,))["status"], "pending")
        self.assertEqual(db.one("SELECT status FROM invoices WHERE id = ?",
                                (self.inv,))["status"], "sent")
        self.assertIsNone(db.one("SELECT paid_at FROM invoices WHERE id = ?", (self.inv,))["paid_at"])

    def test_a_payment_settles_the_invoice_and_starts_the_subscription(self):
        instance_id = billing.confirm_payment(self.inv, self.uid)

        self.assertEqual(instance_id, self.iid, "l'appelant doit savoir quoi construire")
        invoice = db.one("SELECT * FROM invoices WHERE id = ?", (self.inv,))
        self.assertEqual(invoice["status"], "paid")
        self.assertTrue(invoice["paid_at"])
        self.assertEqual(db.one("SELECT status FROM subscriptions WHERE id = ?",
                                (self.sub,))["status"], "active")

    def test_a_replayed_payment_changes_nothing_twice(self):
        billing.confirm_payment(self.inv, self.uid)
        first = db.one("SELECT paid_at FROM invoices WHERE id = ?", (self.inv,))["paid_at"]

        billing.confirm_payment(self.inv, self.uid)

        self.assertEqual(db.one("SELECT paid_at FROM invoices WHERE id = ?",
                                (self.inv,))["paid_at"], first,
                         "un webhook rejoue ne doit pas redater le paiement")

    def test_an_unknown_invoice_is_refused(self):
        with self.assertRaises(ValueError):
            billing.confirm_payment(0, self.uid)

    def test_the_checkout_points_at_the_corridor_itself_in_mock_mode(self):
        checkout = billing.start_checkout(self.uid, self.plan["id"], self.inv)

        self.assertEqual(checkout["mode"], "mock")
        self.assertEqual(checkout["status"], "pending", "un paiement non fait n'est pas payé")
        self.assertTrue(checkout["url"].endswith("/factures/{}".format(self.inv)))

    def test_only_the_payment_starts_provisioning(self):
        """The invariant, read off the code: ordering builds nothing."""
        source = Path(client.__file__).read_text()
        order = source.split("async def instance_create", 1)[1].split("async def instance_detail", 1)[0]
        self.assertEqual(order.count("awaiting_payment"), 1, "la commande s'arrête à l'attente de paiement")
        self.assertNotIn('tasks.start("provision"', order,
                         "une commande ne doit rien construire avant d'être payée")

        payment = source.split("async def invoice_pay(", 1)[1].split("async def account(", 1)[0]
        self.assertIn('tasks.start("provision"', payment,
                      "c'est le paiement qui déclenche la construction")


if __name__ == "__main__":
    unittest.main()
