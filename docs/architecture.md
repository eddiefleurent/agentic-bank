# Architecture

Status: all three demos were deployed and their proofs passed on AWS (us-east-1) on 2026-11-11.
This is a reference and learning architecture, not a production system: see
[Known limits](#known-limits-lean-build) and [what a real bank would add](#what-a-real-bank-would-add).
Diagrams:
[system context](diagrams/01-system-context.png), [read path](diagrams/02-read-path.png),
[transfer](diagrams/03-transfer.png), [dispute](diagrams/04-dispute.png),
[info round and analyst](diagrams/05-info-round.png),
[AML triage](diagrams/06-aml-triage.png) (slice 3, planned until applied).

## What runs where

| Part | Runs on |
| --- | --- |
| Terraform state, CI roles, workload permissions boundary | S3, IAM, GitHub OIDC (`infra/bootstrap`, applied by a human) |
| Deploy pipeline | GitHub Actions, one job on `main`: plan with a read-only role, then apply that plan with the apply role (`infra/app`). The plan file stays on the runner |
| Customer sign-in | Cognito user pool `agentic-bank-customers`: MFA on, TOTP, bank-created users only, Managed Login; clients `agent` (code + PKCE, scopes `read` `propose` `disputes`), `proof` (scripts), `app` (bank app, public, password + TOTP) |
| MCP gateway | AgentCore Gateway `agentic-bank`, JWT authorizer, target `bank` |
| Policy | AgentCore Policy engine `agentic_bank` (ENFORCE), eight Cedar policies |
| Interceptor (customer = subject, revocation check) | Lambda `agentic-bank-interceptor`, reads `agentic-bank-revocations` |
| Tools `get_accounts`, `list_transactions`, `list_payees`, `propose_transfer` | Lambda `agentic-bank-tools`, assumes `agentic-bank-app-data-reader` or `-proposer` per call |
| Tools `explain_charge`, `dispute_transaction`, `get_dispute`, `answer_dispute` | Same Lambda; disputes write through `agentic-bank-app-case-writer` (role policy: case attributes only), tokenize with Comprehend + KMS, start the workflow |
| Card network (Order Insight) | Mock in the zip: `network/orders.json` behind an `OrderInsight` protocol |
| Dispute workflow | Step Functions Standard `agentic-bank-dispute` (JSONata), role `agentic-bank-app-workflow`; worker Lambda `agentic-bank-dispute` assumes `agentic-bank-app-data-writer` (only that Lambda may; the only dispute money writer); review Lambda `agentic-bank-review` (`openai.gpt-oss-120b-1:0` + guardrail `agentic-bank-grounding`) |
| Dispute investigator (**planned, not deployed**) | Strands supervisor + two read-only specialists, behind the reviewer's signature; would run on AgentCore Runtime, invoked by the review Lambda (`REVIEWER=runtime`). Terraform gated by `investigator_enabled = false`. See [investigator.md](investigator.md) |
| Analyst | Lambda `agentic-bank-analyst` behind a function URL (`AWS_IAM`), human role `agentic-bank-app-analyst`, sparse GSI `analyst_queue` |
| PII vault | DynamoDB `agentic-bank-vault`; KMS key `alias/agentic-bank-pii` (bootstrap; only the analyst function may decrypt) |
| Bank app: list, approve, reject, revoke agent | HTTP API `agentic-bank-app` (JWT authorizer, `app` client only) -> Lambda `agentic-bank-approvals`, assumes `agentic-bank-app-approver`; CLI `scripts/bank_app.py` plays the phone |
| Policy assistant `ask_policy`, `lookup_policy` (**implemented, on by default; locally tested, not AWS-verified**) | Bedrock Knowledge Base `agentic-bank-policies` on S3 Vectors, Titan Text Embeddings v2, one chunk per document section; tools Lambda sets the `audience = customer` filter from the caller's identity. `var.knowledge_base` defaults to true (`0c8a16d`); deploy re-indexes. Answer-path numbers are local mode only. See [rag.md](rag.md) |
| Data | DynamoDB `agentic-bank-data` (accounts, transactions, payees, transfers, cases, refund slots, audit), `agentic-bank-revocations`; synthetic customers alex, sam, jordan |

## Patterns

| Pattern | One line |
| --- | --- |
| Agent acts as the customer | OAuth with PKCE + MFA; the token's subject is the customer; scopes per capability |
| Policy outside the model | Cedar at the gateway: tool by scope, amount limits |
| Least privilege per call | Tools assume a role scoped to this customer's rows only |
| Propose, never execute | Agents write requests; only the customer's MFA approval moves money, in one transaction |
| Kill switch | Revoke an agent; its tokens stop working on the next call |
| LLM in a regulated decision | Code facts, model judgment, grounding check, code decides, humans deny |
| Bounded agent, advice only (**planned, not deployed**) | Agents as tools with shared turn, token, tool-call and time caps; output is a typed recommendation that code may ignore (decision 0010) |
| Wait for a human for free | Step Functions task token: no compute while waiting |
| Exactly-once money | Idempotent execution names, conditional writes, atomic limits |

## Risk to control

| Risk | Control | Proof |
| --- | --- | --- |
| Pipeline credentials leak | GitHub OIDC, short-lived credentials, no stored keys | none (config) |
| A pull request deploys, or the pipeline grants itself power | Apply role trusts only `main`; roles it creates must carry `agentic-bank-workload-boundary`; cannot touch CI roles | `proofs/apply_ceiling.sh` |
| Caller is not a signed-in customer | Cognito sign-in (password + TOTP); gateway JWT check | `proofs/customer_token.py` |
| Agent claims to be another customer | Interceptor overwrites `customer` with the token's subject (decision 0001) | `proofs/customer_token.py`, `tests/test_scoping.py` |
| Token without the read grant calls a tool | Cedar, default deny (decision 0002) | `proofs/customer_token.py` |
| Our code reads another customer's rows | Per-call role with session policy `LeadingKeys = subject`; tools role has no table rights (decision 0003) | `proofs/iam_session_policy.py` |
| Asking for another's account reveals it exists | "no such account", never "forbidden" | `proofs/customer_token.py` |
| Agent (or injected text) moves money | Agent can only propose; proposer role may write proposal fields only (decision 0004) | `proofs/money_wall.py` |
| Agent's token approves | Separate API whose authorizer accepts only the `app` client: 401 before our code | `proofs/transfer_approval.py` |
| Stolen session or refresh token approves | Approve needs `auth_time` within 300 s (fresh password + TOTP) | `tests/test_transfers.py` |
| Proposal edited after the customer saw it | Stored == recomputed == sent `request_hash`, checked in the transaction condition | `proofs/transfer_approval.py` |
| Double click, replay, concurrent approvals | One `TransactWriteItems` conditioned on `PENDING`: exactly one debit | `proofs/transfer_approval.py` |
| Over-limit transfer | Cedar `amount_cents <= 100000`, and again in the tool | `proofs/transfer_approval.py` |
| Agent learns or redirects the destination account | Payees by ID, bank-owned rows, account number tokenized (decision 0006) | `proofs/transfer_approval.py` |
| Compromised agent keeps working | Customer revokes in the bank app; interceptor refuses tokens with `auth_time <= revoked_at` on the next call (decision 0005) | `proofs/revoke_agent.py` |
| Stolen access token | Expires in 15 minutes; revocation cuts it at once, and the cutoff only moves forward | `proofs/revoke_agent.py` |
| Injected merchant text steers the agent | Explain output labels text `*_untrusted`; transfer execution still requires customer approval. Read-path proof does not test an external agent's injection resistance | `proofs/explain_charge.py` |
| Stolen access token lives long | 15-minute validity, checked on the real token (`exp - iat = 900`) | `proofs/explain_charge.py` |
| Agent explains another customer's charge | Same scoped reader: "no such transaction" | `proofs/explain_charge.py` |
| Model pays, denies or invents evidence | Model output has no amount or outcome; the model scores C1-C3 without seeing code's scores and any disagreement goes to a person (rule 2); any -1 goes to a person; grounding is a weak signal; code decides (decision 0007) | `proofs/dispute_paths.py`, `tests/test_decide.py` |
| A refund is paid twice | One transaction conditioned on the case status; refund row `attribute_not_exists`; replay returns the stored result (decision 0008) | `proofs/dispute_paths.py`, `tests/test_settle.py` |
| Concurrent disputes break the auto-refund cap | Slot claim inside the same transaction (2 per 90 days) | `proofs/refund_cap.py` |
| Agent side writes money through the dispute path | Tools assume `agentic-bank-app-case-writer`. The role policy allows only case attributes, so omitting the session Policy still cannot write `balance_cents` or forge a charge. `agentic-bank-app-data-writer` is assumable only by the dispute worker. `LeadingKeys` is still only the per-call session policy: a compromised tools process that omits Policy can write another customer's case attributes. It cannot write a balance or a ledger row. | `proofs/money_wall.py` |
| Agent side replays an analyst's approval | The wait's task token sits on the case row, which the tools can read, and they may call `SendTaskSuccess`. The worker refuses a staff refund unless the case carries `analyst_reason`, which only the analyst function's role may write | `tests/test_iam_boundaries.py` checks the allowlist; no AWS proof |
| Customer never answers / analyst forgets | 120 s question timeout -> analyst (rule 8); analyst wait expires after 1 h, leaving a stuck case without an alarm | `proofs/dispute_paths.py --with-timeout` |
| Customer PII reaches the model, logs or history | Detected PII tokenized in the tools Lambda (Comprehend + 7-digit rule), originals KMS-encrypted in the vault (decision 0009) | `proofs/analyst_pii.py` |
| Anyone but the analyst reads PII | Key policy denies Decrypt to all but the analyst function; Lambda checks the analyst role; AUDIT row before decrypt | `proofs/analyst_pii.py`, `proofs/apply_ceiling.sh` |
| Pipeline grants itself decrypt | Apply role has no direct `kms:Decrypt` and no `kms:PutKeyPolicy`, so it cannot decrypt the vault or rewrite the key policy. It can still replace the analyst Lambda, whose role can decrypt, and invoke it. Deployment administrators are trusted with that path in this demo. KMS stops other runtime roles; it does not isolate PII from whoever deploys the analyst function. | `proofs/apply_ceiling.sh` |

Run the proofs: see [deploy.md](deploy.md#run-the-proofs).

## Known limits (lean build)

The policy assistant ships in Terraform and was measured in local mode ([rag.md](rag.md)). It is not AWS-verified. Its audience filter is enforced by our code, not IAM, and its score threshold is the local cosine cutoff.

Checked with the awslabs Well-Architected Security MCP server (read-only, SecurityAudit role). Findings not yet fixed are listed below.

- No VPC, private endpoints or egress control; Lambdas call AWS over the public endpoints with TLS.
  A bank uses private subnets, interface endpoints and endpoint policies.
- CloudWatch log groups and Lambda environment variables use AWS-managed keys, not a customer-managed key; a bank uses customer-managed KMS keys.
- The vault table uses the AWS-owned table key by choice: its PII values are already envelope-encrypted with the PII KMS key and the `{case_id, token}` encryption context.
- No WAF, custom login domain, CloudWatch alarms (denied calls, stuck workflows) or X-Ray tracing. No reserved concurrency on the Lambdas.
- GuardDuty, Security Hub, IAM Access Analyzer and Macie are not enabled.
- DynamoDB deletion protection is off so that teardown works; a bank turns it on.
- Audit is one structured log line per tool call (subject, client_id, tool; 30-day retention), not a
  queryable, fail-closed store.
- Revocation is per customer and agent client: it cuts all of that customer's agents on the `agent` client.
- Transfers: no push notification, device binding or CIBA; password sign-in, not SRP; no idempotency key
  on proposals; no daily or velocity limit; no ACH file, settlement or returns; adding payees not built.
- Bank app API is public without WAF (throttled 10 burst / 5 rps); the `app` token also carries
  `aws.cognito.signin.user.admin`.
- Seed rows are create-only (`ignore_changes = [item]`): to change one, delete it and re-apply.
- Disputes: no Reg E / Reg Z case clocks, denial notices or chargebacks; grounding thresholds carried over, and a probe showed
  grounding alone does not separate an invented rationale (decision 0007); the account admin can
  still edit the key policy or Lambda code (no separate audit account).
- PII detection is probabilistic: missed names/addresses can reach the model and history. The
  deterministic backstop covers only runs of 7+ digits with optional single spaces or hyphens,
  and skips a run that is exactly a hyphenated date.
  Canary checks demonstrate those fixtures, not universal detection or absence from future logs.
- Resuming a wait consumes the task token before SendTaskSuccess. If the send fails, the token
  stays consumed and the caller hears "unconfirmed", never a blind retry. If that send really
  failed, the case sits until the wait's 1 h timeout. No reconciler.
- The case-writer may name `status` and `amount_cents`, which transfer rows also use, and
  `LeadingKeys` is only its session policy. It cannot name `request_hash`, so an edited amount
  fails the approval hash (decision 0004); resetting an executed transfer to PENDING would still
  need the customer's fresh approval inside the 30-minute expiry. A bank splits case and money tables.
- Plan role trust matches any branch (`StringLike` on the subject prefix) and apply trusts `main` only: a lab-account assumption, not an enterprise planning boundary.
  The plan role has AWS `ReadOnlyAccess`, so it can read customer rows and the Terraform state. The
  deploy has no approval step: whoever can push to `main` can deploy.
- The deploy log prints the plan (12-digit numbers masked). It shows resource names and IDs; in a
  public repository those logs are public.
- A compromised tools process can read a waiting case's task token and resume the workflow itself.
  It cannot get a refund that way (see the risk table), but it can force the customer's own case to
  close or fail. A bank keeps task tokens where the agent-side code cannot read them.
- One AWS account, no AWS Organization or service control policies, one region: no separation of duties between deployers, key administrators and
  auditors, no tested restores or recovery plan (PITR is on for the data and vault tables, not revocations). CloudTrail and API access logs exist, but nothing alerts on them. The dispute reviewer has a 150-case synthetic evaluation set ([evaluation](evaluation.md)): one author, no second labeler, and a measured 7% false-refund rate on cases that should not auto-refund. No drift monitoring.
- The proposer role's IAM limits attribute names but not the sort key, so it could overwrite the customer's own ACCT or PAYEE row.
- No per-customer rate limits. `assume_role` per call, not cached.
- Seed rows are in Terraform state (synthetic only). State bucket uses the AWS-managed key and has no S3 access logging.
- `agent` client is confidential; a public PKCE client would suit any MCP client.
- Data-reader trust includes the account root for the IAM proof; a bank would use a break-glass role.

## What a real bank would add

The proofs show that each listed control works on a handful of synthetic fixtures. They do not show
that the system is secure, complete or compliant. The full list is under
[Known limits](#known-limits-lean-build) above. The largest gaps:

| Area | Here | A real bank adds |
| --- | --- | --- |
| Accounts | One AWS account; its admins can reach everything | Separate workload, security, audit and log accounts under an organization, with service control policies and separation of duties |
| Network | Public endpoints, no VPC | Private subnets, interface endpoints with endpoint policies, egress control |
| Edge | Public gateway and API, light throttling | WAF, bot and rate controls per customer, a custom domain |
| Keys and data | One customer-managed key (the PII vault); AWS-managed keys for logs, Lambda environment and state; PITR on the data and vault tables, restores never tested | Customer-managed keys for logs, Lambda environment and state, key administrators separated from data users, secret rotation (the agent client secret is never rotated here), tested restores |
| Resilience | One region, no recovery plan; deletion protection off so teardown works | A tested disaster recovery plan with stated recovery time and data loss targets, usually a second region; deletion protection on data tables; reserved concurrency on the Lambdas |
| Monitoring | Log lines with 30-day retention; no alarms, no X-Ray tracing | Alarms on denials, failed or stuck workflows and key policy changes; GuardDuty, Security Hub, IAM Access Analyzer and Macie enabled; X-Ray tracing; tamper-evident audit in a separate account; on-call and runbooks |
| Identity | Password + TOTP for three seeded users | Identity proofing and onboarding, phishing-resistant sign-in, device binding, account recovery |
| Payments and fraud | A balance update in one table; a $1,000 cap | A real ledger and payment rails, velocity and daily limits, fraud scoring, step-up for new payees |
| The model | Nemotron 3 Super classifies disputes (gpt-oss-120b still answers policy questions and runs the investigator; [evaluation.md](evaluation.md));  a 150-case synthetic evaluation set and an offline gate; the grounding threshold was swept and found to be a weak lever, so it is unchanged; an agentic investigator is written but not deployed ([investigator.md](investigator.md)) | Model risk management: independent validation, an evaluation set, monitoring of drift and override rates, documented human oversight |
| Compliance | None of it | Legal and compliance review (dispute timelines and notices, privacy, card data scope, record retention), third-party risk review of agent and model vendors |
| Assurance | Unit tests, proof scripts and a read-only Well-Architected Security review (awslabs MCP server, SecurityAudit role) | Threat model, penetration test, load and failure testing, change approval |
