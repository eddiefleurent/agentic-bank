# 0008. Exactly-once settlement and atomic refund limits

Status: accepted, deployed

## Context
A refund can be triggered by an automatic decision, a retried Lambda, a replayed workflow step or an
analyst clicking twice. Each must pay at most once, and the "2 automatic refunds per 90 days" cap must
hold when three disputes settle at the same moment.

## Decision
- One worker Lambda (`agentic-bank-dispute`) is the only dispute money writer. It assumes
  `agentic-bank-app-data-writer` with a session policy pinned to the customer's partition.
- Settlement is one `TransactWriteItems`: case `status IN (REVIEWING, ANALYST_REVIEW) -> REFUNDED`,
  balance `+ amount` (read from the TXN row, never from the caller), refund row `attribute_not_exists`,
  and for automatic refunds a `REFUNDQ#1|2` slot claimed only if absent or older than 90 days.
- A cancelled transaction whose case is already REFUNDED returns the stored result (replay); a lost
  slot race retries the other slot once; no free slot routes to the analyst.
- One case per transaction (`dsp_<transaction_id>`). The tool writes the case conditionally, then
  starts the workflow with a name stored on the case, so a retry after a crash restarts the same
  execution (`ExecutionAlreadyExists` is success). The name carries a random suffix because Standard
  execution names stay taken for 90 days and the fixtures are reset between proof runs.
- The tools Lambda assumes `agentic-bank-app-case-writer`, not the settlement writer. The case-writer
  role policy allows only case attributes, so omitting the session Policy still cannot write
  `balance_cents` or forge a charge. `agentic-bank-app-data-writer` is assumable only by the dispute
  worker. `LeadingKeys` is still only the per-call session policy: a tools process that omits Policy
  can write another customer's case attributes. It cannot write a balance or a ledger row.
- The task token of a waiting case sits on the case row, and the tools can read it and call
  `SendTaskSuccess`. So the worker refuses a staff refund unless the case carries `analyst_reason`,
  an attribute only the analyst function's role may write. A forged resume can still close or fail
  the customer's own case.

## Consequences
- Proven: `proofs/dispute_paths.py` (repeat filing = one case, one refund row, balance moved once),
  `proofs/refund_cap.py` (3 concurrent -> exactly 2 refunded, 1 analyst, 2 refund rows),
  `proofs/analyst_pii.py` (second analyst decision 409), `proofs/money_wall.py` (case writer denied).
- Seed rows are now create-only (`ignore_changes = [item]`): an apply no longer resets balances.
- The slots are per customer, not per card or merchant; a bank tunes limits by risk segment.
