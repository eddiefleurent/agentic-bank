# 0006. Payee account numbers tokenized

Status: accepted, deployed

## Context
A transfer needs the payee's full account number, but anything the agent, the logs or the API can see
can leak. The agent should be able to name a payee, never learn or supply where money goes.

## Decision
- Payees are rows the bank owns (`PAYEE#<id>`: name, bank, routing number, last 4, `account_token`).
  The full account number is in no table, log or response; `tok_...` stands for it in the (not built)
  ACH origination system.
- The agent proposes by `payee_id` only, and only among the customer's own payees (another customer's
  payee gets the same "no such account or payee" as a missing one). It cannot type an account number.
- The bank app shows the payee from the bank's own row, not from the proposal, and the agent's memo is
  labelled unverified.

## Consequences
- Adding a payee is out of scope (seeded by Terraform); a bank would require step-up MFA and a cooling-off
  period for a new payee, the classic fraud path.
- Synthetic routing number 990000026 has a valid check digit and never routes.
