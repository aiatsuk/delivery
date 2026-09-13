# Check source: PAY-142 payment retry

## Raw source

PR #67 — Retry payments on provider timeout

Adds a bounded retry loop around the provider charge, records every attempt in the new
payment_requests table, and simplifies the payment-events consumer.

Ticket PAY-142 (docs/PAY-142.md), verbatim:

    The provider times out on ~0.4% of charges. Today the payment fails and the buyer retries manually.

    Expected behaviour:
    - On a provider timeout the service retries the charge automatically, up to 3 attempts.
    - A buyer must never be charged twice for one order.
    - The local payment status must converge with the provider ledger.

## Resolved inputs

| Kind | Reference | Location | Authority |
|---|---|---|---|
| pr | #67 | https://example.test/pr/67 | implementation |
| diff | before..repo | actual-diff.json | implementation |
| ticket | PAY-142 | docs/PAY-142.md | intent |
| design | payment state machine | docs/payments-design.md | context |

## Reference documents

| ID | Kind | Title | Location | Defines |
|---|---|---|---|---|
| REF-1 | ticket | PAY-142 Retry charges on provider timeout | docs/PAY-142.md | retry limit and the never-charge-twice rule |
| REF-2 | design | Payment state machine | docs/payments-design.md | invariants I-1, I-2, I-3 |
| REF-3 | code | Retry implementation | src/payments/retry.py:11 | per-attempt idempotency key |
| REF-4 | code | Payment events consumer | src/payments/events.py:3 | consumer applies events unconditionally |

## Stated intent

Retry a timed-out charge up to three times, never charge a buyer twice, keep local status
converging with the provider ledger.

## Reconstructed intent

The diff adds a bounded retry loop, generates a fresh idempotency key inside the loop, persists
every attempt in payment_requests, and removes the applied-once guard from the events consumer.

## Divergences

- REF-1 requires at most one charge per order, but src/payments/retry.py:11 generates a new
  idempotency key on every attempt, so the provider cannot deduplicate the retry.
- The PR calls the consumer change a simplification; REF-2 invariant I-3 requires consumers to be
  idempotent because delivery is at least once.
- Neither the PR nor the ticket mentions reconciliation for attempts that time out after the
  provider already charged.

## Unavailable context

- No provider sandbox transcript was available, so provider-side deduplication behaviour is taken
  from REF-1 rather than observed.
