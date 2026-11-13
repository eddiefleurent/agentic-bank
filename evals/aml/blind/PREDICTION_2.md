# Two-stage triage, round 2: prediction written before the run

This was written and committed before any round-2 code existed or ran. The round-1 prediction (`PREDICTION.md`) stays as it was. Round 1 missed its deep-recall prediction: 62.5% against the 90% predicted.

## What changed and why
- **The finding from round 1.** Stage 2 caught 5 of the deep suspicious cases that the single call had closed. The "new evidence only" rule then blocked each of those escalations, because the agents cited transactions the single call had also seen. It had misread them.
- **More deep cases.** `deep_cases.json` adds 20 new hard cases (10 suspicious, 10 benign). A separate model wrote them blind from public typologies, with the deciding evidence away from the alerted account. Combined with our 16 deep cases, that makes 36 deep cases (18 suspicious, 18 benign).
- **More runs.** There are 5 runs per setting, not 3, because the single call's deep recall moved 21 points between two runs of the same code.

## Overturn rules, all scored from the same stage-2 outputs
In every rule, stage 2 runs only when the single call recommends close. All cited ids must be ones the agents actually read.
- **A, new evidence (round 1):** the agents escalate and cite at least one transaction that was not in the single call's evidence pack.
- **B, followed the money. This is the primary rule:** the agents escalate and cite at least one transaction on an account other than the alerted customer's.
- **C, any valid escalate:** the agents escalate and pass the normal code checks.

## Noise rule
For each measure, the "noise band" is the spread (max minus min) of the single call's 5 per-run values. A difference between reviewers is counted only if it is larger than the noise band.

## Predictions for rule B (means over 5 runs)
| Set | Measure | Prediction |
|---|---|---|
| 18 deep suspicious | Recall | two-stage-B ≥ single call + 10 points, and the gap is larger than the noise band |
| 25 shallow benign | False escalations | two-stage-B ≤ single call + 10 points |
| 18 deep benign | False escalations | two-stage-B ≤ single call + 15 points |
| 20 new blind deep cases only | Recall | two-stage-B > single call (the direction alone, since the set is small) |
| Rule ordering | Deep recall | A ≤ B ≤ C. On false escalations, C is the worst. |

## Kill rule
If rule B does not beat the single call on deep recall by 10 points or more (beyond the noise band), **or** it adds more than 10 points of shallow false escalations, we stop recommending two-stage. The docs will say so.
