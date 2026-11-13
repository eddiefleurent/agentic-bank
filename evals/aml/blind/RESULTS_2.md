# Two-stage triage, round 2: results against the prediction

Run 2026-11-13. Model `openai.gpt-oss-120b-1:0` on Bedrock (us-east-1), model calls only.
`PREDICTION_2.md` (commit 6a03817), `PREDICTION.md` and both cases files were not changed.
Raw runs: `evals/aml/results/round2/`. Scoring: `evals/aml/round2.py`, output in `score.json`.

## What was run

- Set: our deep variant (66 cases) plus the 20 blind deep cases, in one store: 86 cases, 224 alerts,
  7 of the 20 blind-deep cases needed a synthetic `EXT` alert. 36 deep cases (18 suspicious, 18 benign).
  Deep means our 16 deep cases plus the 20 blind-deep cases.
- Two-stage ran 5 times. Stage 1 is the single call. The stage-2 raw result (recommendation, cited
  ids, read ids, owner of each cited id, pack ids) was stored for every case, and rules A, B and C
  were scored offline from the same runs. The single call is stage 1 of those runs.
- **Shallow suspicious recall comes from 2 runs, not 5.** The owner asked for a speed-up part-way
  through: runs 1 and 2 include the 25 shallow suspicious cases, runs 3 to 5 do not. Every other
  group has all 5 runs. No prediction line uses shallow suspicious recall. Single call and rules A
  to C score 96%, 96%, 96% and 100% on it (2 runs). A partly finished run 3 was stopped and
  discarded when the change came in, and runs 3 to 5 were started again.
- Spend: $2.22 for the 5 kept runs, plus about $0.4 for the discarded partial run. Under the $8 limit.
  All 5 runs were done.

## Prediction against actual (means over 5 runs)

Noise band = max minus min of the single call's 5 per-run values (PREDICTION_2 definition).

| Set | Measure | Prediction | Actual | Verdict |
|---|---|---|---|---|
| 18 deep suspicious | Recall | B at least single + 10 points, and the gap larger than the noise band | B 80.0%, single 68.9%, gap +11.1 points; noise band 16.7 points | **Not met** (gap is over 10 but inside the noise band) |
| 25 shallow benign | False escalations | B at most single + 10 points | B 36.0%, single 35.2%, +0.8 | Met |
| 18 deep benign | False escalations | B at most single + 15 points | B 61.1%, single 60.0%, +1.1 | Met |
| 20 blind deep | Recall | B above single | B 78.0%, single 76.0%, +2 points (one case in two of five runs) | Met (direction only) |
| Rule ordering | Deep recall A <= B <= C | 78.9% <= 80.0% <= 88.9% | Met |
| Rule ordering | C worst on false escalations | C is worst | C 58.4% shallow benign and 73.3% deep benign, against 38.4% and 61.1% for A, 36.0% and 61.1% for B | Met |

## Kill rule

The rule: stop recommending two-stage if B does not beat the single call on deep recall by 10 points
or more (beyond the noise band), OR it adds more than 10 points of shallow false escalations.

- Deep recall gain: +11.1 points, noise band 16.7 points. The gain is not beyond the noise band.
- Shallow false escalations added: +0.8 points. That condition is false.

The first condition is true, so **the kill rule fires. We stop recommending two-stage.**

## Per run

Deep suspicious recall (18 cases):

| Reviewer | r1 | r2 | r3 | r4 | r5 | Mean |
|---|---|---|---|---|---|---|
| Single call | 72.2% | 77.8% | 61.1% | 72.2% | 61.1% | 68.9% |
| Two-stage A | 83.3% | 83.3% | 83.3% | 77.8% | 66.7% | 78.9% |
| Two-stage B | 83.3% | 83.3% | 83.3% | 83.3% | 66.7% | 80.0% |
| Two-stage C | 94.4% | 83.3% | 88.9% | 94.4% | 83.3% | 88.9% |

Shallow benign false escalations (25 cases):

| Reviewer | r1 | r2 | r3 | r4 | r5 | Mean |
|---|---|---|---|---|---|---|
| Single call | 28.0% | 32.0% | 48.0% | 36.0% | 32.0% | 35.2% |
| Two-stage A | 28.0% | 36.0% | 56.0% | 36.0% | 36.0% | 38.4% |
| Two-stage B | 28.0% | 32.0% | 48.0% | 36.0% | 36.0% | 36.0% |
| Two-stage C | 56.0% | 60.0% | 64.0% | 64.0% | 48.0% | 58.4% |

Deep benign false escalations (18 cases):

| Reviewer | r1 | r2 | r3 | r4 | r5 | Mean |
|---|---|---|---|---|---|---|
| Single call | 66.7% | 50.0% | 55.6% | 66.7% | 61.1% | 60.0% |
| Two-stage A | 72.2% | 50.0% | 55.6% | 66.7% | 61.1% | 61.1% |
| Two-stage B | 66.7% | 55.6% | 55.6% | 66.7% | 61.1% | 61.1% |
| Two-stage C | 83.3% | 66.7% | 66.7% | 77.8% | 72.2% | 73.3% |

Recall on the 20 blind-deep cases (10 suspicious):

| Reviewer | r1 | r2 | r3 | r4 | r5 | Mean |
|---|---|---|---|---|---|---|
| Single call | 80.0% | 80.0% | 70.0% | 80.0% | 70.0% | 76.0% |
| Two-stage A | 80.0% | 80.0% | 80.0% | 80.0% | 70.0% | 78.0% |
| Two-stage B | 80.0% | 80.0% | 80.0% | 80.0% | 70.0% | 78.0% |
| Two-stage C | 100.0% | 80.0% | 80.0% | 100.0% | 90.0% | 90.0% |

## Noise and flips

Noise band (single call, max minus min over 5 runs): deep recall 16.7 points, shallow benign false
escalations 20.0, deep benign false escalations 16.7, blind-deep recall 10.0.

Per-case flip rate (share of cases whose single-call outcome changes across runs): **24.4% of all
cases (21 of 86), 33.3% of the 36 deep cases (12 of 36).** Shallow suspicious cases count over 2
runs only. The same model on the same case disagrees with itself in one case out of three on deep
cases. That is why a gap of 11 points does not count.

## Verdict, in plain words

Two-stage with rule B is not shown to beat the single call, so we stop recommending it.

- Rule B raised deep recall from 68.9% to 80.0%. The single call's own five runs ranged from 61.1%
  to 77.8%, so the gain is smaller than the noise. Run 5 shows it: the single call and B both
  scored 61.1% and 66.7%, and in run 4 B was 11 points up.
- B did what it was built to do on false escalations. It added 0.8 points on shallow benign and 1.1
  on deep benign. That is the whole benefit of the rule: it is nearly free in false escalations.
- The old "new evidence" rule A gave 78.9%, one point under B. Round 1's idea that A was too strict
  is mostly not supported: B and A decide almost the same cases.
- Rule C (escalate on any valid agent escalate) is the only variant with a gap beyond the noise:
  88.9% against 68.9%, +20 points. It pays +23 points of shallow false escalations (58.4% against
  35.2%) and +13 on deep benign. The prediction named B as the primary rule, and that is what the
  kill rule judges. C is a different trade, not a rescue of the prediction.
- On the 20 blind-deep cases B gained 2 points, one case in two runs. That meets "above" and shows
  nothing.
- Cost: stage 2 ran on 147 of 355 case-runs (41%) and cost about $0.010 and 31 s each. Two-stage
  averaged $0.0062 per case against about $0.002 for stage 1 alone.

## What this does not show

- Five runs and 36 deep cases. The noise band is wide because the model's answer on a deep case
  often changes between runs.
- Only gpt-oss-120b. A stronger stage-1 model might shrink the gap in either direction.
- The blind-deep cases are new customers added to the store, so their neighbourhood is clean.
- Shallow suspicious recall: 2 runs only (see above).
