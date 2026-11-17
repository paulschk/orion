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


def subscribe(user_id: int, plan_id: int, instance_id: int | None = None) -> tuple[int, int]:
    """Create the subscription and its first invoice. Returns (subscription, invoice)."""
    plan = db.one("SELECT * FROM plans WHERE id = ?", (plan_id,))
    if plan is None:
        raise ValueError("unknown plan")
    subscription_id = db.execute(
        "INSERT INTO subscriptions(user_id, plan_id, instance_id, status, started_at, renews_at) "
        "VALUES(?, ?, ?, 'active', ?, ?)",
        (user_id, plan_id, instance_id, now(), days_from_now(plan["period_days"])),
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


def start_checkout(user_id: int, plan_id: int) -> dict:
    """Integration point for a real payment provider.

    With ORION_BILLING_MODE=mock this returns a pseudo checkout that the caller
    immediately treats as paid. To go live, create a Stripe Checkout Session
    here and return its URL instead - nothing else in the code path changes.
    """
    if settings.billing_mode != "mock":
        raise NotImplementedError(
            "Set ORION_BILLING_MODE=mock, or implement the Stripe branch of start_checkout()."
        )
    return {"mode": "mock", "status": "paid", "url": None}


def reminder_needed(subscription_row) -> bool:
    if subscription_row is None:
        return False
    return (subscription_row["renews_at"] or "") <= now()
