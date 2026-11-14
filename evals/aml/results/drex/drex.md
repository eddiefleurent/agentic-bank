# Drex on the full AML case set

One OpenRouter pass of `nace-ai/drex-max-finance` on tokenized snapshots. Not an eval campaign. No Bedrock calls.

136 cases (base 50, deep 66, blind-deep 20). Reported cost $0.069230. p≥0.5 counts as Drex escalate. Cascade-with-Drex: auto-escalate → escalate; likely-close → close; unsure → stored graph recommendation, else stored baseline. A stored row is used only when its alert id and verdict match the current case. Latency mean 1.13s, p50 1.15s (136 calls).

## Same 66 deep cases

Drex, stored Reflex, baseline, and graph each have a row here, so the columns share a case set. Cascade is cascade-with-Drex.

| Metric | Drex | Reflex | Baseline | Graph | Cascade |
| --- | --- | --- | --- | --- | --- |
| n | 66 | 66 | 66 | 66 | 66 |
| Recall on suspicious | 85% (69% to 93%) | 97% (85% to 99%) | 88% (73% to 95%) | 94% (80% to 98%) | 88% (73% to 95%) |
| Recall, shallow | 92% (75% to 98%) | 100% (87% to 100%) | 100% (87% to 100%) | 96% (80% to 99%) | 96% (80% to 99%) |
| Recall, deep | 62% (31% to 86%) | 88% (53% to 98%) | 50% (22% to 78%) | 88% (53% to 98%) | 62% (31% to 86%) |
| Recall, blind-deep | n/a | n/a | n/a | n/a | n/a |
| False-escalation on benign | 33% (20% to 50%) | 42% (27% to 59%) | 52% (35% to 67%) | 64% (47% to 78%) | 36% (22% to 53%) |
| False-escalation, shallow | 12% (4% to 30%) | 24% (11% to 43%) | 40% (23% to 59%) | 56% (37% to 73%) | 16% (6% to 35%) |
| False-escalation, deep | 100% (68% to 100%) | 100% (68% to 100%) | 88% (53% to 98%) | 88% (53% to 98%) | 100% (68% to 100%) |
| Share reaching the expensive agent | 0% | 0% | 100% | 100% | 17% |
| $ per case | $0.0006 | $0.0008 | $0.0025 | $0.0115 | $0.0034 |

## Full set

Drex, stored Reflex, and the Drex cascade cover every case above. Stored base baseline joined 0 current base cases (label ids overlap the old file, alert ids do not). Graph joined the deep cases only. Blind-deep has no stored graph or baseline, so an unsure band there is not an escalation.

| Metric | Drex | Reflex | Baseline | Graph | Cascade |
| --- | --- | --- | --- | --- | --- |
| n | 136 | 136 | 66 | 66 | 136 |
| Recall on suspicious | 88% (78% to 94%) | 97% (90% to 99%) | 88% (73% to 95%) | 94% (80% to 98%) | 82% (72% to 90%) |
| Recall, shallow | 92% (81% to 97%) | 100% (93% to 100%) | 100% (87% to 100%) | 96% (80% to 99%) | 88% (76% to 94%) |
| Recall, deep | 62% (31% to 86%) | 88% (53% to 98%) | 50% (22% to 78%) | 88% (53% to 98%) | 62% (31% to 86%) |
| Recall, blind-deep | 90% (60% to 98%) | 90% (60% to 98%) | n/a | n/a | 70% (40% to 89%) |
| False-escalation on benign | 21% (13% to 32%) | 31% (21% to 43%) | 52% (35% to 67%) | 64% (47% to 78%) | 21% (13% to 32%) |
| False-escalation, shallow | 8% (3% to 19%) | 22% (13% to 35%) | 40% (23% to 59%) | 56% (37% to 73%) | 10% (4% to 21%) |
| False-escalation, deep | 100% (68% to 100%) | 100% (68% to 100%) | 88% (53% to 98%) | 88% (53% to 98%) | 100% (68% to 100%) |
| Share reaching the expensive agent | 0% | 0% | 100% | 100% | 21% |
| $ per case | $0.0005 | $0.0007 | $0.0025 | $0.0115 | $0.0019 |

## Bands

Truly suspicious is the share of that band whose label is suspicious.

| Band | Suspicious | Benign | Truly suspicious |
| --- | --- | --- | --- |
| auto-escalate | 50 | 11 | 82% |
| unsure | 15 | 13 | 54% |
| likely-close | 3 | 44 | 6% |
| no opinion | 0 | 0 | n/a |

## Suspicious cases in the likely-close band

The cascade sends these to the close queue. A person still reads them.

- deep/LABEL-003 hops p=0.0261596135
- deep/LABEL-020 hops p=0.0461212574
- deep/LABEL-049 hops p=0.0261596135

## Stored runs

- base baseline: `evals/aml/results/baseline.cases.jsonl` (not used, 0 alert ids match)
- deep baseline: `evals/aml/results/two-stage/deep-baseline-r3.cases.jsonl` (used, 66 cases)
- deep graph: `evals/aml/results/two-stage/deep-agents-graph-r3.cases.jsonl` (used, 66 cases)
- reflex: `evals/aml/results/reflex/reflex.cases.jsonl` (used, 136 cases)

`evals/aml/results/2b/agents-graph-r*.cases.jsonl` is an older fingerprint of these 66 deep cases and was not mixed in. Blind-deep has no stored graph or baseline; an unsure band there adds no agent cost and is not an escalation.
