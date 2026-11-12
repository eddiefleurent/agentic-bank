# 0010. Dispute investigator: agents as tools, bounded, advice only

Status: proposed, not deployed (code, tests and gated Terraform exist; see [investigator.md](../investigator.md))

## Context
The dispute reviewer (decision 0007) is one model call over a case file. Richer cases need evidence
the case file does not carry (account history, dispute rules, later a policy knowledge base). An
agent can fetch it, but a loop adds cost, latency, variance and new ways to fail.

## Decision
- **Agents as tools**, Strands SDK: a supervisor calls an evidence gatherer and a policy checker,
  each an `Agent` wrapped with `as_tool()`. Chosen over a **Swarm** (agents hand off to each other
  freely: no single owner of the answer, hard to bound and audit) and over a **Graph** (a fixed DAG
  of agents: predictable, but here the order is trivial, and a Graph earns its place once steps
  branch on results). Agents as tools keep one owner of the final answer and one call tree to
  trace and bound.
- **Hard bounds across the whole tree:** Strands request `limits` (turns, total tokens,
  structured-output attempts) are shared by nested agents; a hook caps tool calls; a timer cancels
  after 90 s. Tools are read-only closures; history goes through the per-customer data-reader role.
- **Advice only:** the output model (pydantic) has no amount and no outcome. Its criteria and the
  classifier's two flags pass the classifier's checks; `decide.py` decides, unchanged, so a flag
  blocks an auto-refund here too (rule 9). Any failure is rule 1 (analyst).
- **Same signature** as `review.run_review`; `REVIEWER` selects (classifier stays the default).
- **Hosting:** AgentCore Runtime (direct code deployment), invoked by the existing review Lambda,
  because Step Functions' AgentCore integration targets harnesses, not custom Runtimes.

## Consequences
- Measured on 8 synthetic cases x 2 (same model): ~4x latency, ~7x tokens, more rule-1 failures
  (6 vs 1 of 16), no refund outside code's rules. For today's case file the classifier is better.
- The recommendation is not persisted on the case row; an analyst sees it only in logs. A bank
  stores every turn and tool result for audit and model risk review.
- Deployment needs bootstrap changes (boundary: S3 code read, AgentCore log group) not made here.
