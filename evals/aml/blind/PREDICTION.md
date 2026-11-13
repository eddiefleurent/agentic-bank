# Two-stage triage: prediction written before the run

This was written and committed before any two-stage code existed or ran. Its purpose is to stop us from fitting a story to the results afterwards.

## The design under test
1. A single model call reviews every alert.
2. If it says **escalate**, the case goes to an analyst.
3. If it says **close**, the graph-exploring agents take a second look.
   - They can overturn the close only by citing a transaction they read that was **not** in the single call's evidence pack.
   - Code checks this.
   - If they find no new evidence, the close stands, and an analyst still signs off.

## The blind set
- 20 cases (10 suspicious, 10 benign) are in `cases.json`.
- A different model wrote them from public money-laundering typologies.
- That model never saw this repo, our prompts, our rules or our deep cases.
- The cases are small: at most 15 transactions and 3 customers each.

## Predictions (3 runs each, means)
| Set | Measure | Prediction |
|---|---|---|
| Deep variant (our cases) | Recall on the 8 deep suspicious cases | two-stage ≥ 90% |
| Deep variant (our cases) | False escalations on the 25 shallow benign cases | two-stage ≤ 40% (single call: 33%) |
| Blind set | Recall on the 10 suspicious cases | two-stage ≥ single call, gain ≤ 10 points (the cases are mostly shallow) |
| Blind set | False escalations on the 10 benign cases | two-stage ≤ single call + 10 points |

## Kill rule
If two-stage gains nothing on the blind set **and** adds more than 10 points of false escalations, we stop recommending it. The docs will then say it only helped on cases we wrote ourselves.
