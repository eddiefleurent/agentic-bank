# 0001. The customer's agent connects through an MCP gateway with the customer's own token

Status: accepted, deployed

## Context
Customers run their own agents (Claude and others). The bank cannot control the agent, its prompt or
its model, so nothing the agent says can be trusted to identify the customer or limit what it does.

## Decision
- The agent is an MCP client of one public endpoint: AgentCore Gateway `agentic-bank`.
- The customer signs in to Cognito (Managed Login, password + TOTP, bank-created users only) through
  OAuth authorization code + PKCE. The access token's subject is the customer; its scopes
  (`<gateway URL>/read`, `/propose`, `/disputes`) are what they granted. Access token 15 minutes, refresh 30 days.
- The gateway's JWT authorizer checks signature, issuer, expiry and `client_id` (Cognito access tokens
  carry no `aud`). The agent never holds an AWS credential or a bank-side API key.
- A REQUEST interceptor Lambda sets `arguments.customer` to the token's subject, overwriting anything
  the agent sent. Tool schemas have no `customer` field.

## Consequences
- Proven by `proofs/customer_token.py`: no token = 401; sending another customer's subject changes nothing.
- The `agent` app client is confidential (has a secret) because the connector flow accepted it; a public
  PKCE-only client would work for any MCP client without handing out a secret. Open choice.
- Interceptor decodes the token without re-verifying it; this is safe only because the authorizer runs first.
- Skipped, and how a bank would do it: no VPC, private endpoints or egress control (the gateway is public
  by design and calls Lambda over IAM; a bank keeps Lambdas in private subnets with endpoint policies); no
  WAF or custom login domain; no fail-closed activity table (one structured log line per call instead,
  subject, client_id, tool). CloudTrail and API access logs exist alongside it. Only the PII vault uses a customer-managed KMS key; PITR is on for the data and vault tables (restores never tested).
