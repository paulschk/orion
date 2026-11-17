# Billing (mocked)

`ORION_BILLING_MODE=mock` is the only implemented mode. It is deliberately
honest about what it does:

* choosing a plan **provisions immediately** and records an invoice with status
  `sent`;
* no payment is requested, no card is stored, no external call is made;
* an admin marks invoices `paid` or `void` from `/admin/invoices`;
* the tariff page says so, and the client invoice page says so.

That is a complete, auditable paper trail — which is what a proof of concept
needs — without pretending to be a payment system.

## Going live with Stripe

One function is the whole integration point:

```python
# orion/billing.py
def start_checkout(user_id: int, plan_id: int) -> dict:
    if settings.billing_mode != "mock":
        raise NotImplementedError(...)
    return {"mode": "mock", "status": "paid", "url": None}
```

To go live:

1. Set `ORION_BILLING_MODE=stripe`, `ORION_STRIPE_SECRET_KEY`,
   `ORION_STRIPE_WEBHOOK_SECRET`.
2. Create a Stripe Checkout Session in `start_checkout()` and return its `url`.
3. Redirect the client to it instead of provisioning immediately.
4. Add a webhook route that, on `checkout.session.completed`, performs the
   provisioning step currently done synchronously in
   `routers/client.py::instance_create` and calls `billing.mark_paid()`.

Everything else already exists: `subscriptions` carries `renews_at`,
`invoices` carries `number`/`status`/`paid_at`, and plans carry
`price_cents`/`currency`/`period_days`.

## Invoice numbering

`ORION-<year>-<sequence>`, computed from the count of that year's rows and
collision-checked before insert.

## What is deliberately absent

Proration, taxes/VAT handling, dunning, refunds, credit notes, and a
customer-facing PDF. None of them belong in a proof of concept, and each is a
self-contained addition to `billing.py` when the offer becomes real.
