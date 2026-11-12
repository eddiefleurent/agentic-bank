# 0005. Revocation by sign-in time

Status: accepted, deployed

## Context
The gateway validates access tokens locally, so revoking a refresh token in Cognito leaves the agent's
current access token working for up to 15 minutes. A kill switch must bite on the next call, and must not
need the (possibly compromised) agent's cooperation.

## Decision
- The bank app's `POST /agent/revoke` writes `(customer, agent client_id, revoked_at)` to table
  `agentic-bank-revocations`. There is no revoke tool on the gateway.
- The interceptor reads that row (consistent `GetItem`) on every `tools/call` and `tools/list`, and
  refuses with JSON-RPC `-32001` when the token's `auth_time <= revoked_at`. An unreadable row or a lookup error also refuse: fail closed. A missing `auth_time` refuses only once a
  revocation row exists for that customer and client; with no row the call is allowed.
- Restore is a new sign-in: its later `auth_time` passes, while every older token, refreshed or not,
  stays dead.

## Consequences
- Proven by `proofs/revoke_agent.py`: same token and a refreshed token refused, other customers
  unaffected, a new sign-in works, the old token stays dead.
- Cost: one ~5 ms read per tool call.
- Per customer and agent client, not per connection: revoking cuts all of that customer's agents that use
  the `agent` client. A bank with many agent vendors would key it per client or per grant.
- The refusal is a JSON-RPC error with HTTP 200: the interceptor cannot make the gateway send 401.
