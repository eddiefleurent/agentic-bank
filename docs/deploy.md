# Develop, deploy and prove

## Develop

Needs [uv](https://docs.astral.sh/uv/) (it installs Python 3.14) and Terraform 1.11 or newer.

```
uv sync
uv run ruff check && uv run ruff format --check && uv run ty check && uv run pytest -q
terraform fmt -check -recursive infra
```

## Deploy

You need an AWS account you control, admin credentials for the one-time bootstrap, and your own
GitHub repository with Actions enabled. The account must be able to invoke the Bedrock models
`openai.gpt-oss-120b-1:0` in `us-east-1`. The stack uses fixed resource names and `us-east-1`: one
stack per account, not a multi-environment template. Estimate the [cost](#cost) first.

1. **Bootstrap, once, from your machine (local state).** It creates the state bucket, the two CI
   roles, the permissions boundary and the PII key. Copy `infra/bootstrap/terraform.tfvars.example`
   to `terraform.tfvars` and set `github_oidc_subject_prefix` to your repository's OIDC subject
   prefix (the example shows how to read it). It decides which repository may assume the CI roles.
   Set `create_oidc_provider = false` if the account already has the GitHub OIDC provider.
   ```
   terraform -chdir=infra/bootstrap init
   terraform -chdir=infra/bootstrap apply
   ```
   Keep the local state file safe and out of Git (it is ignored). CI never applies this root.
2. **Tell the workflow your account.** In the repository settings add the Actions secret
   `AWS_ACCOUNT_ID` (12 digits). An account ID is not a credential, but as a secret GitHub masks it
   in the run logs, which are public in a public repository. A variable of the same name also works.
3. **Push to `main`** (or run the `deploy` workflow by hand). One job plans with the read-only role,
   then applies that exact plan with the role that trusts only `main`. It runs when `infra/app/`,
   `src/` or the workflow changes. It does not wait for the `ci` workflow, so run the local checks
   first. The plan is printed to the run log with 12-digit numbers masked; it still shows resource
   names and IDs.
4. **Run the deploy a second time** on a new stack. The first run creates the gateway and saves its
   URL in SSM; the second creates what depends on that URL: the agent OAuth client, the scopes and
   the Cedar policies. Until then no tool call is permitted.
5. **Create the customers' logins**: `uv run python scripts/setup_customer.py alex` (likewise `sam`
   and `jordan`), with admin credentials. It sets a random password, enrolls TOTP and writes both to
   `~/.config/agentic-bank/<user>.json` (mode 600) for the proofs. Nothing is printed.
6. **Connect an agent** (optional): add an MCP connector with the gateway URL (Terraform output
   `gateway_url`, also in SSM `/agentic-bank/gateway-url`) and the `agent` OAuth client's ID and
   secret (read them from Cognito). The client's callback URL is fixed to Claude's in
   `infra/app/identity.tf`.

## Run the proofs

Each script checks controls on the deployed stack and prints PASS or FAIL lines. They need admin
AWS credentials and the customer logins from step 5. Each sign-in waits for a new TOTP window, so a
script takes a few minutes. The dispute fixtures are one case per charge: the proofs reset
them themselves (`proofs/_cleanup.py` is the manual reset). Each demo in [demos.md](demos.md) lists its own; all of them:

```
uv run python proofs/customer_token.py
uv run python proofs/iam_session_policy.py
proofs/apply_ceiling.sh
uv run python proofs/explain_charge.py
uv run python proofs/transfer_approval.py alex
uv run python proofs/revoke_agent.py alex
uv run python proofs/money_wall.py
uv run python proofs/dispute_paths.py --keep --with-timeout
uv run python proofs/analyst_pii.py
uv run python proofs/refund_cap.py
```

The proofs write to the deployed stack (transfers, disputes, refunds), so balances drift from the
seed values. The seeded charges are dated November 2026 and the dispute window is 120 days: to run
the dispute proofs after February 2027, change the dates in `infra/app/data.tf` and recreate those
rows.

## Teardown

```
terraform -chdir=infra/app init -backend-config=backend.hcl   # from backend.hcl.example
terraform -chdir=infra/app destroy
terraform -chdir=infra/bootstrap destroy
```

Destroy the app first, with admin credentials. The state bucket has `prevent_destroy`: to delete it,
remove that line and empty the bucket including old versions. The PII key is scheduled for deletion
and stays (unusable, not billed) for 30 days. Stop any running dispute executions first, or wait for
them to time out. Delete `~/.config/agentic-bank/` on your machine.

## Cost

us-east-1, demo traffic; rates checked 2026-11-11. This is an estimate: check current prices.

Idle, the stack costs about $1/month: the PII KMS key ($1.00/month). Everything else bills per request:
AgentCore gateway and policy calls, Lambda, DynamoDB on demand (tables and the GSI), Step Functions
($0.025 per 1,000 transitions; waiting for a human is free), Cognito Essentials (free under 10,000
monthly users), SSM standard parameters (free), CloudWatch Logs (30-day retention, cents), the state
bucket (cents). One dispute costs about $0.005: Comprehend PII ~$0.0003, Nemotron review
(~$0.15/$0.60 per million tokens, 1-2 calls) ~$0.003, grounding check ~$0.001, ~15 transitions $0.0004,
KMS $0.03 per 10,000 calls. A day of proof runs (about 20 disputes) stays under $0.20.
