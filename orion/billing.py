"""Billing, mocked on purpose.

The proof of concept provisions immediately and records a paper invoice. Every
call that would talk to a payment provider is isolated in `start_checkout()` so
swapping in Stripe is a single function, not a refactor. See docs/BILLING.md.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import activity, db
from .config import settings
from .util import days_from_now, now


def _sequence(year: int) -> int:
    count = db.scalar("SELECT COUNT(*) FROM invoices WHERE number LIKE ?", (f"ORION-{year}-%",), 0)
    return int(count) + 1


def next_number() -> str:
    year = datetime.now(timezone.utc).year
    for _ in range(50):
        number = f"ORION-{year}-{_sequence(year):04d}"
        if db.one("SELECT 1 FROM invoices WHERE number = ?", (number,)) is None:
            return number
    return f"ORION-{year}-{int(datetime.now(timezone.utc).timestamp())}"


def create_invoice(user_id: int, amount_cents: int, description: str, *,
                   subscription_id: int | None = None, days: int = 30,
                   status: str = "sent") -> int:
    return db.execute(
        "INSERT INTO invoices(number, user_id, subscription_id, amount_cents, currency, "
        "status, description, issued_at, due_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (next_number(), user_id, subscription_id, amount_cents, settings.currency,
         status, description, now(), days_from_now(days)),
    )


def subscribe(user_id: int, plan_id: int, instance_id: int | None = None, *,
              status: str = "active") -> tuple[int, int]:
    """Create the subscription and its first invoice. Returns (subscription, invoice).

    `status="pending"` is the client's path: the order exists, the invoice is due,
    and nothing is provisioned until the payment is confirmed.
    """
    plan = db.one("SELECT * FROM plans WHERE id = ?", (plan_id,))
    if plan is None:
        raise ValueError("unknown plan")
    subscription_id = db.execute(
        "INSERT INTO subscriptions(user_id, plan_id, instance_id, status, started_at, renews_at) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        (user_id, plan_id, instance_id, status, now(), days_from_now(plan["period_days"])),
    )
    invoice_id = create_invoice(
        user_id, plan["price_cents"],
        f"Abonnement {plan['name']} - {plan['vcpu']} vCPU / {plan['memory_mb']} MB / {plan['disk_gb']} GB",
        subscription_id=subscription_id, days=plan["period_days"],
    )
    activity.log("billing.subscribed", user_id=user_id, target=plan["slug"],
                 detail=f"invoice {invoice_id}")
    return subscription_id, invoice_id


def mark_paid(invoice_id: int, actor_id: int | None = None) -> None:
    db.execute("UPDATE invoices SET status = 'paid', paid_at = ? WHERE id = ?", (now(), invoice_id))
    activity.log("billing.invoice_paid", user_id=actor_id, target=f"invoice:{invoice_id}")


def for_user(user_id: int) -> list:
    return db.query("SELECT * FROM invoices WHERE user_id = ? ORDER BY id DESC", (user_id,))


def for_user_with_context(user_id: int) -> list:
    return db.query(
        "SELECT i.*, p.name AS plan_name, p.slug AS plan_slug, inst.slug AS instance_slug, "
        "       inst.id AS instance_id "
        "FROM invoices i "
        "LEFT JOIN subscriptions s ON s.id = i.subscription_id "
        "LEFT JOIN plans p ON p.id = s.plan_id "
        "LEFT JOIN instances inst ON inst.id = s.instance_id "
        "WHERE i.user_id = ? ORDER BY i.id DESC",
        (user_id,))


def all_invoices(limit: int = 200) -> list:
    return db.query(
        "SELECT i.*, u.email FROM invoices i JOIN users u ON u.id = i.user_id "
        "ORDER BY i.id DESC LIMIT ?", (limit,))


def invoice(invoice_id: int):
    return db.one("SELECT * FROM invoices WHERE id = ?", (invoice_id,))


def start_checkout(user_id: int, plan_id: int, invoice_id: int | None = None) -> dict:
    """Where the money would be taken. One function, one seam.

    In mock mode this hands back the panel's own payment page. The corridor is the
    real one - nothing is provisioned before a payment is confirmed, and the
    invoice, the subscription and the instance all have their own state - only the
    cash is imaginary, and the page says so. To go live, create the provider's
    session here and return its URL; its webhook then calls `confirm_payment()`
    below, which is exactly what the mock's button calls.
    """
    if settings.billing_mode != "mock":
        raise NotImplementedError(
            "Set ORION_BILLING_MODE=mock, or implement the provider branch of start_checkout() "
            "and have its webhook call confirm_payment()."
        )
    return {"mode": "mock", "status": "pending",
            "url": f"/app/factures/{invoice_id}" if invoice_id else None}


def confirm_payment(invoice_id: int, actor_id: int | None = None) -> int | None:
    """A payment came in: the invoice is settled and the subscription starts.

    Returns the instance the invoice was for, so the caller can provision it.
    Idempotent - a provider that retries its webhook settles nothing twice.
    """
    row = invoice(invoice_id)
    if row is None:
        raise ValueError("unknown invoice")
    if row["status"] != "paid":
        db.execute("UPDATE invoices SET status = 'paid', paid_at = ? WHERE id = ?",
                   (now(), invoice_id))
        activity.log("billing.invoice_paid", user_id=actor_id or row["user_id"],
                     target=f"invoice:{invoice_id}")

    subscription = (db.one("SELECT * FROM subscriptions WHERE id = ?", (row["subscription_id"],))
                    if row["subscription_id"] else None)
    if subscription is None:
        return None
    plan = db.one("SELECT period_days FROM plans WHERE id = ?", (subscription["plan_id"],))
    db.execute("UPDATE subscriptions SET status = 'active', renews_at = ? WHERE id = ?",
               (days_from_now(plan["period_days"] if plan else 30), subscription["id"]))
    return subscription["instance_id"]


def reminder_needed(subscription_row) -> bool:
    if subscription_row is None:
        return False
    return (subscription_row["renews_at"] or "") <= now()
