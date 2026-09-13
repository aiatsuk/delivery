# Impact map: PAY-142 payment retry

Derived from the actual change, not from the plan.

## Changed surface

| Path or contract | Status | What changed | Evidence |
|---|---|---|---|
| src/payments/retry.py | modified | bounded retry loop, per-attempt idempotency key, attempt rows | REF-3 |
| src/payments/events.py | modified | applied-once guard removed from the consumer | REF-4 |
| payment_requests table | added | one row per attempt, no uniqueness on order_id | REF-3 |

## Directly affected behavior

| Area | Mechanism | Observable effect | Evidence |
|---|---|---|---|
| charge creation | retry reaches the provider again with a different key | a second provider charge | REF-1, REF-3 |
| payment status | status is written only on the success path | status stays PENDING after an ambiguous timeout | REF-2 |
| ledger writes | consumer applies every delivery | duplicate ledger rows on redelivery | REF-2, REF-4 |

## Indirect and downstream surface

- Reconciliation job: reads payments, not payment_requests, so ambiguous attempts stay invisible.
- Provider rate limits: three immediate attempts with no backoff during a provider incident.
- Support tooling: an order can now map to several idempotency keys.

## Protected invariants

- I-1 one order produces at most one provider charge.
- I-2 local status converges with the provider ledger within one reconciliation cycle.
- I-3 payment_events consumers are idempotent under at-least-once delivery.

## Depth mode

- Mode: deep
- Reason: payments and persistence are critical areas; money is irreversible and the change
  touches the charge path, a new table, and an event consumer.

## Out of scope for this check

- Provider-side pricing and fee calculation: untouched by this diff.
- Front-end checkout rendering: no client surface changed.
