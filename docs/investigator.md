# Dispute investigator (planned, not deployed)

> **Status: planned / not deployed.** Code, tests and Terraform exist; the deployed workflow still
> uses the one-call classifier. `infra/app/investigator.tf` is gated by
> `investigator_enabled = false`. Measured locally against Bedrock only (2026-11-12).

The classifier (`disputes/review.py`) is one model call with no tools. The investigator replaces
that call with a small, bounded agent team, behind the same function signature, so the workflow,
the decision rules and the eval harness do not change:

```text
Review step --> run_investigation(bedrock, model_id, guardrail_id, version, case_file)
                  supervisor (Strands Agent, structured output = Investigation)
                    |-- evidence_gatherer (agent as tool): get_case_facts, get_case_field,
                    |                                      list_recent_transactions   [read-only]
                    '-- policy_checker    (agent as tool): lookup_policy(query)       [read-only]
                  -> pydantic Investigation -> same shape rule, cited-field check and grounding
                     guardrail as the classifier -> ReviewOutcome -> decide.py (code decides)
```

| Piece | Where |
| --- | --- |
| Agents, bounds, tool budget | `src/agentic_bank/investigator/agent.py` |
| Output model (no amount, no outcome) | `src/agentic_bank/investigator/schema.py` |
| Read-only evidence, history via the customer tools' read path | `src/agentic_bank/investigator/evidence.py` |
| Policy seam + synthetic rules | `src/agentic_bank/investigator/policy.py`, `dispute_policy.md` |
| Choosing a reviewer (`REVIEWER` = `classifier`, `investigator`, `runtime`) | `src/agentic_bank/disputes/reviewers.py` |
| AgentCore Runtime entrypoint, build | `src/agentic_bank/investigator/runtime.py`, `scripts/build_investigator.sh` |
| Terraform (not applied) | `infra/app/investigator.tf`; Review payload and timeout in `dispute.asl.json` |
| Tests (scripted model, no AWS) | `tests/test_investigator.py` |
| Side-by-side run | `scripts/compare_reviewers.py` |

## What stays the same

- **Code decides.** The investigator's `recommendation` (`refund_eligible`, `ask_customer`,
  `escalate`) is stored under `review.advice` for the analyst. `decide.py` never reads it; it
  reads the criteria scores, and rule 2 still compares the model's C1-C3 with code's.
- **Same gate.** Its criteria go through the classifier's exact shape rule, the cited-field check
  and the grounding guardrail (`review.verify_review`, shared).
- **Fail closed.** A bound, a timeout, a model error or output that fails the pydantic model
  returns `ReviewOutcome(failure=...)`: rule 1, an analyst. It never raises and never refunds.
- **Money wall untouched.** The agent has no write tool and its Runtime role has no table rights:
  history comes through `agentic-bank-app-data-reader` (GetItem/Query, this customer's partition).

## Bounds (`Bounds` in `agent.py`)

| Bound | Value | Enforced by |
| --- | --- | --- |
| Model turns, all three agents together | 12 | Strands `limits={"turns": ...}`; nested agents-as-tools share the request's budget |
| Tokens, input + output, all agents | 60,000 | Strands `limits={"total_tokens": ...}` (soft: checked between turns) |
| Output tokens per model call | 1,500 | `max_tokens` on the Bedrock model |
| Tool calls (specialists + their tools) | 8 | `ToolBudget` hook: refuses the call, the model is told to answer |
| Bad structured outputs | 2 | Strands `structured_output_attempts` |
| Wall clock | 90 s | a timer sets Strands' `cancel_signal` (the Step Functions task allows 150 s) |
| One model call | 30 s read timeout, 2 attempts | botocore `Config` |

## Model choice

The investigator stays on gpt-oss-120b. Trial on Nemotron 3 Super (2026-11-12, 8 synthetic disputes): tool calling through Strands worked, but 7 of 8 investigations cited field names absent from the case file (failing the grounding-of-citations check) and on the injection dispute it recommended a refund. Re-test before changing it.

## Real local run (Bedrock, us-east-1, 2026-11-12)

Same model for both (`openai.gpt-oss-120b-1:0`; the classifier's default is now Nemotron 3 Super, so this is a separate setting, `investigator_model`), same guardrail,
8 synthetic disputes, run twice (16 per reviewer). `uv run python scripts/compare_reviewers.py`.

| | Classifier | Investigator |
| --- | --- | --- |
| Mean latency | 2.3-3.5 s | 10.6-11.1 s |
| Mean tokens per case | ~1,200 | ~8,900 (8-9 turns, 5-6 tool calls) |
| Model cost per case | ~$0.0004 | ~$0.002 |
| Rule-1 failures (to analyst) | 1 of 16 | 6 of 16 (4 grounding, 2 cited a non-field) |
| Auto-refunds | 7 of 16 | 6 of 16 |
| Same outcome as the classifier | | 13 of 16 |

Every auto-refund, from either reviewer, was a case where code's own facts were all +1 and the
amount was at most $50 (`t_alex_003`, `006`, `009`, `010`): rule 2 and rule 5 held. Disagreements
were one reviewer failing a check and sending a clear case to a person. The prompt-injected statement (`t_alex_010`) and merchant
(`t_alex_003`) changed nothing beyond what the facts support. Small sample: a trend, not a result.

## When an agent beats a fixed workflow, and when it does not

**An agent earns its cost when** the evidence to read is not known in advance: many possible
sources (history, prior disputes, merchant data, policy text that changes), where reading all of
them every time is too slow or too large for one prompt, and where a person would otherwise
"go and look". Agents-as-tools also give each specialist a narrow tool set and a narrow prompt.

**A fixed workflow wins when** the facts are known and few. Today's case file already holds every
fact the criteria need, so the investigator mostly re-fetches what the classifier is simply
given: 4-5x the latency, ~7x the tokens, more chances to fail the grounding check (its rationales
are written after two hand-offs), and less predictable (8-9 turns rather than 1 call). Here, the
classifier is the better production choice; the investigator is the pattern to grow into once
history, prior disputes and the policy knowledge base carry real signal.

Trade-offs to weigh: latency (the customer waits on the Review step), cost per case, variance
(model risk validation is harder for a loop than for one call), failure rate (each failure is an
analyst's time), and auditability (a bank must store every turn and tool result; here only one
summary log line per run is written).

## The policy seam

`PolicySource.lookup(query, k) -> list[Passage]` is the only thing `lookup_policy` calls.
`INVESTIGATOR_POLICY` picks the source. `local` (default, no AWS): `LocalPolicy` scores sections of
`dispute_policy.md` by shared words. `knowledge_base`: `KnowledgeBasePolicy` calls the RAG
`lookup_policy` ([rag.md](rag.md)), which reads customer and internal documents from the Bedrock
Knowledge Base; its citations become `doc_id/section-slug` ids. `policy_refs` accepts `DR-n` or
that form, nothing else. An unavailable knowledge base is a tool error the model sees, never an
empty "no rule applies". Not wired in Terraform yet: the Runtime would need `INVESTIGATOR_POLICY`,
`KNOWLEDGE_BASE_ID` and `bedrock:Retrieve` on that Knowledge Base.

## Deploying it (not done)

1. `scripts/build_investigator.sh` (arm64 wheels, Python 3.14, ~30 MB zip).
2. Bootstrap (by a human): the workload boundary must allow `s3:GetObject` on the code bucket and
   logs under `/aws/bedrock-agentcore/runtimes/*`; the apply role must be able to create the bucket.
3. `investigator_enabled = true`, review the plan, apply. The review Lambda switches to
   `REVIEWER=runtime` and its timeout to 140 s; data-reader trusts the Runtime role.

Cost: AgentCore Runtime bills per second of active CPU and memory per session; waiting on the model
is mostly I/O, and nothing is billed while idle, so idle cost is about $0 (plus cents a month for
the S3 object). A bank would also put the Runtime in a VPC (`network_mode = "VPC"`) with interface
endpoints, store each turn for audit, and track override and failure rates per reviewer.
