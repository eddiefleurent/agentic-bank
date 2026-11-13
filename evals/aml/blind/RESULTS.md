# Two-stage triage: results against the prediction

Run 2026-11-13. Model `openai.gpt-oss-120b-1:0` on Bedrock (us-east-1), model calls only, 3 runs per
reviewer per variant. The prediction in `PREDICTION.md` (commit 75b9ccb) was not changed.
Raw runs: `evals/aml/results/two-stage/`. Every result carries a code fingerprint that matches the
code at the time of the run. The earlier 2b runs did not match the changed code, so baseline and
agents-graph were re-run on the deep variant.

## Prediction against actual

Means over 3 runs. The run values are in brackets.

| Set | Measure | Prediction | Actual | Verdict |
|---|---|---|---|---|
| Deep variant | Recall on the 8 deep suspicious cases | two-stage 90% or more | 62.5% (62.5, 75, 50) | **Not met** |
| Deep variant | False escalations on the 25 shallow benign cases | two-stage 40% or less (single call 33%) | 37.3% (40, 36, 36); single call 34.7% | Met |
| Blind set | Recall on the 10 suspicious cases | two-stage at least the single call, gain at most 10 points | 100% against 100%, gain 0 | Met |
| Blind set | False escalations on the 10 benign cases | two-stage at most single call + 10 points | 26.7% against 26.7% (+0) | Met |

## Kill rule

The rule: stop recommending two-stage if it gains nothing on the blind set AND adds more than 10
points of false escalations.

- Gain on the blind set: none (recall 100% for both).
- Added false escalations on the blind set: 0 points.

The first condition is true. The second is false. **The kill rule does not fire.**

## Verdict, in plain words

Two-stage is not killed. It is also not shown to help.

On the blind set the single call already escalated all 10 suspicious cases in every run. Stage 2
had nothing to fix. The blind set cannot show a gain, so it neither supports nor refutes the idea.
The cost there was small: the same false escalations, 4.9x the cost per case ($0.0039 against
$0.0008) and 1.6x the latency at p50.

On our deep cases the headline prediction failed. We predicted 90% recall on the 8 deep suspicious
cases. Two-stage got 62.5%, against 58% for the single call and 92% for the graph agents alone. Over
3 runs there were 24 deep suspicious case-runs:

- 11 escalated at stage 1.
- 3 stage-1 failures, which escalate.
- 10 stage-1 closes went to stage 2. Stage 2 rescued 1 (new evidence). It left 9 closes standing:
  5 where the agents escalated but cited nothing new outside the pack, 2 where stage 2 failed, and
  2 where the agents also closed.

The "new evidence" rule is what held recall down. It blocked 5 correct escalations. In those 5 the
agents said escalate, but none of the risk-factor ids they cited was a transaction they read outside
the single call's evidence pack. The rule did its job on false escalations (shallow benign 37% against
57% for the graph agents alone). The cost is missed true positives.

What the data supports:

- Two-stage keeps the single call's false-escalation level (37% against 35%) and adds about 4
  points of recall on the deep set and none on the blind set. That is within the run-to-run range.
- The graph agents alone have the best recall on deep cases (92%) but escalate 57% of shallow
  benign cases and 40% of blind benign cases.
- No variant of this design reached both high deep recall and a low false-escalation rate.

## Numbers behind the table

Deep variant, 66 cases, mean (min-max). `deep-compare.md` has the full table.

| Metric | Single call | Two-stage | Graph agents |
|---|---|---|---|
| Recall, all 33 suspicious | 88% | 90% (88-91) | 94% |
| Recall, 8 deep | 58% (50-62) | 62% (50-75) | 92% (88-100) |
| False escalations, 25 shallow benign | 35% (32-40) | 37% (36-40) | 57% (52-64) |
| Failures (became escalate) | 15% | 18% | 25% |
| Latency p50 | 5.6 s | 7.2 s | 30.6 s |
| Cost per case | $0.0025 | $0.0064 | $0.0115 |

Blind set, 20 cases (10 suspicious, 10 benign), mean (min-max). `blind-compare.md` has the full table.
Typology accuracy is n/a: the blind typologies are free text.

| Metric | Single call | Two-stage | Graph agents |
|---|---|---|---|
| Recall | 100% | 100% | 100% |
| False escalations | 27% (20-30) | 27% (20-30) | 40% (30-50) |
| Failures (became escalate) | 5% | 5% | 7% |
| Latency p50 | 3.9 s | 6.4 s | 28.3 s |
| Cost per case | $0.0008 | $0.0039 | $0.0084 |

On the blind set, "shallow" and "deep" rows in the generated tables are not meaningful: every blind
case has `depth="blind"`, which the harness groups with deep. Read the "all" rows.

Which stage decided (3 runs, both variants):

| Outcome | Deep (198 case-runs) | Blind (60 case-runs) |
|---|---|---|
| Stage 1 escalated | 100 | 33 |
| Stage 1 failed (escalate) | 35 | 3 |
| Stage 2 closed too | 28 | 12 |
| Stage 2 failed, close stands, flagged | 13 | 6 |
| Stage 2 escalated, no new evidence, close stands | 18 | 4 |
| Stage 2 escalated with new evidence | 4 | 2 |

Stage 2 ran on 63 of 198 deep case-runs (32%) and 24 of 60 blind case-runs (40%). When it ran it
cost about $0.012 (deep) and $0.008 (blind) and took about 31 s and 26 s. Stage 1 cost $0.0025 and
5.8 s (deep), $0.0008 and 4.4 s (blind).

## Blind import

- 20 cases imported mechanically into the base dataset: 49,863 transactions, 157 alerts.
- Alerted customers that raised no rule alert and got a synthetic `EXT` alert: **5 of 20**.
- The model never sees labels, summaries, `decisive_evidence` or case keys. A test checks it.

## What this does not show

- 20 blind cases and 3 runs. One case moving changes a rate by 10 points on the benign side.
- The blind cases are small (at most 15 transactions, 3 customers) and their customers are new, so
  nothing in the background dataset touches them.
- Re-run drift is large. The single call's deep recall was 79% in the 2b run on the same cases and
  logic, and 58% now. The graph agents were 96% then, 92% now. Model behaviour changed between
  days, or the sample is too small. We did not find a code cause. Differences of a few points between
  reviewers are inside this drift.
- Spend: about $4.81 for the kept runs. Earlier smoke runs are not counted.
