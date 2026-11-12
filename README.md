# Agentic Bank

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/eddiefleurent/agentic-bank)

**Banking where customers already are: in their AI agent.**

Customers already run errands through personal agents like Claude, ChatGPT, OpenClaw, Muse and
GrokBot. Agentic Bank lets those agents do what used to need the website or app: explain a charge,
send money, dispute a purchase. The agent acts as the customer, can read and propose, never moves
money on its own, and can be cut off with one revoke command in the bank app. Inside the bank, an LLM helps review card
disputes, but code makes every decision and a person makes every "no".

It is a fictional US bank on synthetic data, built on AWS with Terraform and Python: an AgentCore
Gateway with Cedar policy, Cognito with TOTP, Lambda tools, DynamoDB with an envelope-encrypted
PII vault, and a Step Functions dispute workflow with a human in the loop. Ten proof scripts in `proofs/` ran against the deployed stack (us-east-1, 2026-11-11); some controls are checked only by config or unit tests. The agent tested so far is
Claude, through a custom connector.

![Architecture: the customer's AI agent calls an AgentCore Gateway (JWT check, Cedar policy), then
an interceptor Lambda that sets the customer to the token's subject, then a tools Lambda that
assumes a per-call IAM role limited to that customer's DynamoDB rows. Dispute statements are
tokenized into a KMS-encrypted PII vault and run through a Step Functions workflow where a Bedrock
model scores and code decides. The customer approves transfers through a separate bank app API; a
dispute analyst decides through an IAM-authenticated function. Cognito handles sign-in; CloudTrail
and CloudWatch Logs record calls.](docs/diagrams/architecture.png)

## Ask Claude to dispute a charge

A real session, trimmed (full transcripts in [docs/demos.md](docs/demos.md)):

```text
You:    I never got the HOMEBREW SUPPLY order, dispute it. Call me at 512-555-0142.
Claude: -> bank___dispute_transaction {"transaction_id": "t_alex_006", "reason_code": "not_received", ...}
           stored and reviewed as: "I never got it. ... call me at <PHONE_ae9f4d7a>."
        -> bank___get_dispute {"case_id": "dsp_t_alex_006"}     7 s later: REFUNDED 34.00
        Refunded $34.00 to Everyday Checking: the merchant never shipped it.

You:    Send $500 to my landlord for December rent.
Claude: -> bank___propose_transfer {"payee_id": "pay_alex_landlord", "amount_cents": 50000, ...}
        -> {"status": "pending_approval", "expires_at": "2026-11-11T19:55:04Z"}
        I've set up $500.00 to Riverside Property Mgmt. Approve it in your Agentic Bank app.
```

| The customer asks | What happens |
| --- | --- |
| "What's this $12.99 charge?" | Card-network order data (mock): item, device, 3-D Secure. Merchant text is labeled untrusted |
| "Send $500 to my landlord." | The agent writes a `PENDING` proposal; money moves only after the customer approves in the app with a fresh password + TOTP |
| "I never got this order, dispute it." | detected PII is tokenized, a model scores the facts, code refunds small clear cases or sends the case to a person |
| "Cut off my agent." | The customer revokes it in the bank app; tokens from earlier sign-ins fail on the next call |

## How it works

- **The agent is the customer, nothing more.** The customer signs in once at Cognito (password +
  TOTP) and grants scopes. The agent's OAuth token carries the customer as its subject.
- **Policy lives outside the model.** The AgentCore Gateway checks the token, an interceptor
  overwrites `customer` with the token's subject, and Cedar (ENFORCE, default deny) allows each tool
  only with its scope and caps transfers at $1,000.
- **AWS enforces the data boundary.** The tools Lambda has no table rights. Per call it assumes a
  role whose session policy allows only this customer's partition (`dynamodb:LeadingKeys`).
- **Agents propose, people approve.** Transfers execute in a separate API that accepts only the
  bank app's tokens, inside one DynamoDB transaction, exactly once.
- **The model advises, code decides.** Disputes run in Step Functions: code builds the facts, a
  Bedrock model scores them, code refunds or asks one question, and only an analyst can deny.
  Waiting for a person costs nothing (a task token; the analyst wait times out after 1 h).

## Which control stops which risk

![One tool call, five checks: a hijacked agent sends propose_transfer for $5,000 naming another
customer. 1, the gateway JWT authorizer stops missing or expired tokens with
401. 2, the request interceptor overwrites the customer with the token's subject and refuses revoked
agents. 3, Cedar policy denies a missing scope or an amount over $1,000. 4, the per-call IAM session
policy makes AWS deny reads of another customer's rows. 5, the agent's only tool that touches a transfer writes a PENDING
proposal; money moves only when the customer approves in the bank app with a fresh password + TOTP,
in one DynamoDB transaction.](docs/diagrams/tool-call-controls.png)

| Risk | Control | Proof |
| --- | --- | --- |
| Agent claims to be another customer | Interceptor sets `customer` to the token's subject | [`customer_token.py`](proofs/customer_token.py) |
| Our own code reads another customer's rows | Per-call IAM session policy, `LeadingKeys` = subject | [`iam_session_policy.py`](proofs/iam_session_policy.py) |
| Agent, or injected text, moves money | Agent-side role may write proposal fields only | [`money_wall.py`](proofs/money_wall.py) |
| Proposal edited after the customer saw it | Approval sends the hash of what was shown; checked in the transaction | [`transfer_approval.py`](proofs/transfer_approval.py) |
| Double click or replayed approval | One transaction conditioned on `PENDING`: exactly one debit | [`transfer_approval.py`](proofs/transfer_approval.py) |
| Compromised agent keeps working | Revoke in the app; tokens from earlier sign-ins fail on the next call | [`revoke_agent.py`](proofs/revoke_agent.py) |
| Model pays, denies or invents evidence | Model output has no amount or outcome; disagreement goes to a person | [`dispute_paths.py`](proofs/dispute_paths.py) |
| Concurrent disputes beat the refund cap | Slot claim in the same transaction (2 auto-refunds per 90 days) | [`refund_cap.py`](proofs/refund_cap.py) |
| PII reaches the model or the logs | Detected PII (Comprehend + a digit rule) is tokenized; originals are KMS-encrypted in the vault | [`analyst_pii.py`](proofs/analyst_pii.py) |
| Anyone but the analyst reads PII | Key policy allows decrypt only to the analyst function, which writes an audit row first | [`analyst_pii.py`](proofs/analyst_pii.py) |
| The deploy pipeline grants itself power | GitHub OIDC; apply role trusts only `main`; roles it creates carry a permissions boundary | [`apply_ceiling.sh`](proofs/apply_ceiling.sh) |

The full table, with residual risks, is in [docs/architecture.md](docs/architecture.md#risk-to-control);
the reasoning is in the [decision records](docs/decisions/README.md).

## Try it

Needs [uv](https://docs.astral.sh/uv/), Terraform 1.11+, an AWS account you control and a GitHub
repository with Actions. Local checks need no AWS:

```sh
uv sync
uv run ruff check && uv run ty check && uv run pytest -q
```

To deploy: bootstrap once from your machine, push to `main` (GitHub Actions plans and applies),
create the demo logins, then point an MCP client at the gateway. Idle cost is about $1/month (the
PII KMS key); a day of proof runs stays under $0.20. Step by step, with proofs, teardown and cost:
[docs/deploy.md](docs/deploy.md).

## Docs

- [Architecture](docs/architecture.md): what runs where, patterns, risk to control, known limits
- [Demos](docs/demos.md): full transcripts and each request step by step
- [Deploy and prove](docs/deploy.md): bootstrap, pipeline, proofs, teardown, cost
- [Decision records](docs/decisions/README.md): nine short ADRs
- [Sequence diagrams](docs/diagrams/): read path, transfer, dispute, analyst round
- [Ask DeepWiki](https://deepwiki.com/eddiefleurent/agentic-bank): a generated wiki of this repo you can ask questions

This is a reference build on synthetic data, not a production bank; see the
[known limits](docs/architecture.md#known-limits-lean-build) and
[what a real bank would add](docs/architecture.md#what-a-real-bank-would-add).

Found a vulnerability? See [SECURITY.md](SECURITY.md). License: [MIT](LICENSE).
