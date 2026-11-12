# 0004. Agents propose, customers approve: money moves only on MFA approval

Status: accepted, deployed

## Context
An agent that can move money is one prompt injection away from moving it. The control must not depend
on the model behaving, and the agent's credential must not be able to reach the step that executes.

## Decision
- The agent gets one write tool, `propose_transfer` (scope `propose`, Cedar caps it at $1,000.00). It
  writes a `PENDING` `XFER#` row through `agentic-bank-app-proposer`, whose policy allows only `PutItem`
  of the proposal's attributes: no `balance_cents`, no `description` (no ledger row), no `UpdateItem`.
- Approval lives on a separate door: HTTP API `agentic-bank-app` with a JWT authorizer that accepts only
  the public `app` client. The agent's token gets 401 there before our code runs; the `app` token is
  refused at the MCP gateway (403, not in `allowed_clients`).
- Approve requires a fresh sign-in (`auth_time` within 300 s, so a refreshed token cannot approve) and a
  `request_hash` that matches both the stored hash and one recomputed from current fields: what the
  customer saw is what executes.
- Execution is one `TransactWriteItems`: XFER `PENDING -> EXECUTED` (all fields and expiry in the
  condition), balance debit (condition `balance >= amount`), ledger row keyed by the transfer ID.
  Replays and concurrent approvals race on the first condition; DynamoDB lets exactly one win.

## Consequences
- Proven by `proofs/transfer_approval.py` (approve once, replay 409, two concurrent approvals one debit,
  edited amount 409, Cedar refusal over $1,000, insufficient funds keeps it PENDING, both walls) and
  `proofs/money_wall.py` (AWS denies the proposer anything but a proposal).
- The dispute case-writer may also name `status` and `amount_cents` on any row, but never
  `request_hash`, so an edited transfer fails the hash check above. A reset of `status` to PENDING
  still needs the customer's fresh approval before expiry; a bank keeps cases out of the money table.
- No idempotency key: an agent retry makes a second proposal that needs its own approval.
- Skipped, and how a bank would do it: no push notification, device binding or CIBA (the agent tells the
  customer); password sign-in, not SRP; no daily or velocity limit; no ACH file, settlement or return codes;
  the public API has no WAF; the `app` token also carries `aws.cognito.signin.user.admin`.
