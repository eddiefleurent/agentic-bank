# Reflex on the full AML case set

One OpenRouter pass of `morph/reflex` on tokenized snapshots. Not an eval campaign. No Bedrock calls.

136 cases (base 50, deep 66, blind-deep 20). Reported cost $0.089023. p≥0.5 counts as Reflex escalate. Cascade: auto-escalate → escalate; likely-close → close; unsure → stored graph recommendation, else stored baseline. A stored row is used only when its alert id and verdict match the current case.

## Same 66 deep cases

Baseline, graph, and Reflex each have a row here, so the columns share a case set.

| Metric | Reflex | Baseline | Graph | Cascade |
| --- | --- | --- | --- | --- |
| n | 66 | 66 | 66 | 66 |
| Recall on suspicious | 97% (85% to 99%) | 88% (73% to 95%) | 94% (80% to 98%) | 97% (85% to 99%) |
| Recall, shallow | 100% (87% to 100%) | 100% (87% to 100%) | 96% (80% to 99%) | 100% (87% to 100%) |
| Recall, deep | 88% (53% to 98%) | 50% (22% to 78%) | 88% (53% to 98%) | 88% (53% to 98%) |
| Recall, blind-deep | n/a | n/a | n/a | n/a |
| False-escalation on benign | 42% (27% to 59%) | 52% (35% to 67%) | 64% (47% to 78%) | 45% (30% to 62%) |
| False-escalation, shallow | 24% (11% to 43%) | 40% (23% to 59%) | 56% (37% to 73%) | 28% (14% to 48%) |
| False-escalation, deep | 100% (68% to 100%) | 88% (53% to 98%) | 88% (53% to 98%) | 100% (68% to 100%) |
| Share reaching the expensive agent | 0% | 100% | 100% | 26% |
| $ per case | $0.0008 | $0.0025 | $0.0115 | $0.0040 |

## Full set

Reflex and the cascade cover every case above. Stored base baseline joined 0 current base cases (label ids overlap the old file, alert ids do not). Graph joined the deep cases only. Blind-deep has no stored graph or baseline, so an unsure band there is not an escalation.

| Metric | Reflex | Baseline | Graph | Cascade |
| --- | --- | --- | --- | --- |
| n | 136 | 66 | 66 | 136 |
| Recall on suspicious | 97% (90% to 99%) | 88% (73% to 95%) | 94% (80% to 98%) | 91% (82% to 96%) |
| Recall, shallow | 100% (93% to 100%) | 100% (87% to 100%) | 96% (80% to 99%) | 98% (90% to 100%) |
| Recall, deep | 88% (53% to 98%) | 50% (22% to 78%) | 88% (53% to 98%) | 88% (53% to 98%) |
| Recall, blind-deep | 90% (60% to 98%) | n/a | n/a | 60% (31% to 83%) |
| False-escalation on benign | 31% (21% to 43%) | 52% (35% to 67%) | 64% (47% to 78%) | 24% (15% to 35%) |
| False-escalation, shallow | 22% (13% to 35%) | 40% (23% to 59%) | 56% (37% to 73%) | 16% (8% to 29%) |
| False-escalation, deep | 100% (68% to 100%) | 88% (53% to 98%) | 88% (53% to 98%) | 100% (68% to 100%) |
| Share reaching the expensive agent | 0% | 100% | 100% | 26% |
| $ per case | $0.0007 | $0.0025 | $0.0115 | $0.0022 |

## Bands

Truly suspicious is the share of that band whose label is suspicious.

| Band | Suspicious | Benign | Truly suspicious |
| --- | --- | --- | --- |
| auto-escalate | 57 | 10 | 85% |
| unsure | 11 | 24 | 31% |
| likely-close | 0 | 34 | 0% |
| no opinion | 0 | 0 | n/a |

## Suspicious cases in the likely-close band

The cascade sends these to the close queue. A person still reads them.

- none

## Stored runs

- base baseline: `evals/aml/results/baseline.cases.jsonl` (not used, 0 alert ids match)
- deep baseline: `evals/aml/results/two-stage/deep-baseline-r3.cases.jsonl` (used, 66 cases)
- deep graph: `evals/aml/results/two-stage/deep-agents-graph-r3.cases.jsonl` (used, 66 cases)

`evals/aml/results/2b/agents-graph-r*.cases.jsonl` is an older fingerprint of these 66 deep cases and was not mixed in. Blind-deep has no stored graph or baseline; an unsure band there adds no agent cost and is not an escalation.
