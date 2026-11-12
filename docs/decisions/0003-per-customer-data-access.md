# 0003. Per-customer data access with session-scoped roles

Status: accepted, deployed

## Context
The tools Lambda is our code. A bug (a wrong key, a missing filter) must not be able to read another
customer's data, so the wall cannot be application logic alone.

## Decision
- One DynamoDB table `agentic-bank-data`: partition key `customer` (the Cognito subject), sort key `sk`
  (`ACCT#...`, `TXN#<account>#<date>#<id>`). Money is integer cents.
- The tools role `agentic-bank-app-tools` has no table rights. Per call it assumes
  `agentic-bank-app-data-reader` (GetItem, Query; no Scan) with session name `c-<subject>` and an inline
  session policy: `dynamodb:LeadingKeys` must equal `[subject]` and be present. AWS intersects the policy
  with the role, so another partition returns AccessDeniedException.
- An account that is not in the caller's partition is answered `{"error": "no such account"}`, never
  "forbidden", so existence does not leak. `account_id` is validated against `^[a-z0-9_]{1,32}$` (no `#`).

## Consequences
- Proven by `proofs/iam_session_policy.py` (alex's session: own partition ok, sam's partition and a
  Scan denied by AWS) and `proofs/customer_token.py` (end to end through the gateway). Unit tests:
  `tests/test_scoping.py`.
- Cost: one STS AssumeRole per tool call (credentials are not cached; 900 s duration).
- Data-reader trust includes the account root so an admin can run the IAM proof; a bank would trust a
  break-glass role instead.
- Skipped, and how a bank would do it: AWS-owned encryption key on the data table (PITR is on for the data and vault tables, restores never tested), seed rows sit in Terraform
  state (synthetic data only). A bank uses a customer-managed key with a key policy, tested restores, and a
  data-reader role that only the tools role can assume. The apply role is capped by the permissions
  boundary `agentic-bank-workload-boundary` (`proofs/apply_ceiling.sh`).
