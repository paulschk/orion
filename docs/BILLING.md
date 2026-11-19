# The payment corridor

An order follows four steps, and the panel shows where it is at each one. Exactly
one step is imaginary in this proof of concept, and the page that takes the
payment says so in as many words.

| Step | What happens | Where it lives |
| --- | --- | --- |
| 1. Order | plan and template chosen, a name and an SSH port reserved | `POST /app/instances` |
| 2. Invoice | a subscription (`pending`) and its first invoice (`sent`) are created. **Nothing is built.** | `billing.subscribe(..., status="pending")` |
| 3. Payment | the invoice is settled: `paid`, with `paid_at` | `billing.start_checkout()` then `POST /app/factures/<id>/payer` |
| 4. Build | the subscription becomes `active`, the instance leaves `awaiting_payment` for `provisioning`, the job starts | `billing.confirm_payment()` + the provision task |

The client sees the whole thing on one page, `/app/factures/<id>`: what was
ordered, what is due, when it is due, what "pay" does here, and what follows.
While the invoice is unpaid the instance exists as a row and nothing else - its
page says so, and the status is `À payer` rather than a spinner with nothing
behind it.

**An unpaid order is not lost.** The page stays reachable, so a client who closes
the tab, or a payment that fails halfway with a real provider, comes back to the
same place and the machine is built then.

## Going live with a provider

Two functions, and the corridor between them does not change:

```python
# orion/billing.py
def start_checkout(user_id, plan_id, invoice_id=None) -> dict:
    """Returns {"url": ...} - where the client goes to pay."""

def confirm_payment(invoice_id, actor_id=None) -> int | None:
    """Payment received. Returns the instance to build. Idempotent."""
```

To go live:

1. Set `ORION_BILLING_MODE=stripe`, `ORION_STRIPE_SECRET_KEY`,
   `ORION_STRIPE_WEBHOOK_SECRET`.
2. In `start_checkout()`, create the provider's session and return its `url`.
   The payment page already reads `checkout["url"]`; with a provider it becomes
   the redirect instead of the local button.
3. Add a webhook route that, on `checkout.session.completed`, calls
   `confirm_payment(invoice_id)` and then starts the provisioning job - the same
   two lines the button's handler runs. It is written to be idempotent, because
   providers retry webhooks.

Everything else already exists: `subscriptions.renews_at`, `invoices.number`,
`invoices.status`, `invoices.due_at`, `invoices.paid_at`, and plans carry
`price_cents` / `currency` / `period_days`.

## What is deliberately absent

Proration, taxes/VAT handling, dunning, refunds, credit notes, and a
customer-facing PDF. None of them belong in a proof of concept, and each is a
self-contained addition to `billing.py` when the offer becomes real.

## Invoice numbering

`ORION-<year>-<sequence>`, computed from the count of that year's rows and
collision-checked before insert.
