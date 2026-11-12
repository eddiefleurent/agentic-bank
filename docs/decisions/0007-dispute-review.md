# 0007. Dispute review: code facts, model judgment, grounding, code decides

Status: accepted, deployed

## Context
A card dispute needs judgment on free text ("it never came"), which is what a model is good at, but a
refund or a denial is a regulated decision. The model must not be able to pay, deny, or invent evidence.

## Decision
- Code builds the case file: the TXN row, the card network's order (Order Insight mock, merchant text
  under `*_untrusted`, cut to 200 chars). Code scores C1 delivery, C2 device and C3 3-D Secure itself and
  keeps those scores out of the case file, so the model scores the same criteria without seeing them.
- The review Lambda (`nvidia.nemotron-super-3-120b`; first built on `openai.gpt-oss-120b-1:0`, see docs/evaluation.md; temperature 0, no data access) scores only the
  criteria code names, writes short rationales and may raise two flags (`concedes_charge`,
  `instructs_reviewer`). Its one tool, `submit_review`, does nothing: it is forced with `toolChoice`
  so the answer arrives as tool input that matches a JSON Schema. Its output has no amount and no outcome field;
  anything but exactly that shape after one retry is rule 1 (analyst). Criteria that do not apply are
  dropped, and every cited field must exist in the case file.
- Bedrock Guardrails contextual grounding (`ApplyGuardrail`, grounding >= 0.65, relevance >= 0.7) scores
  the rationale against the case file. Code then decides with eight ordered rules (`disputes/decide.py`):
  model and code disagree on C1-C3, any -1, `other`, or no order -> analyst; all +1 and <= $50.00 -> auto
  refund, unless the model raised a flag (rule 9, analyst); some 0 on round 0 -> one template
  question; silence for 120 s -> analyst (rule 8). A flag can only take an auto-refund away.
- Only a person says no: the workflow can refund or route to the analyst queue, never close a case.
- Step Functions Standard (JSONata) runs it; both human waits use task tokens (no compute while waiting).

## Consequences
- Measured 2026-11-11: constrained decoding (`outputConfig.textFormat`) gave 3 valid answers of 8 (the
  rest ran into whitespace); prompt-only plus strict parsing gave 8 of 8. Constrained decoding is off.
  Superseded 2026-11-12: the answer is forced through a tool instead (`toolChoice`), with the same
  strict validation after it; see [evaluation](../evaluation.md) for the before/after numbers.
- Real runs: `t_alex_006` grounding 0.88 / relevance 1.0 (auto refund), `t_alex_008` 0.98 / 1.0 (rule 3,
  analyst). A one-off probe on `t_alex_007` did NOT separate a supported rationale (grounding 0.70,
  relevance 0.40) from an invented one ("signed for at the door": 0.71 / 0.10). Grounding is therefore a
  weak signal here, not the control: rule 2 (the model's blind C1-C3 must match code's, and code's win)
  and "no -1 is ever automatic" are. The
  thresholds were not re-calibrated on a larger set: a known gap.
- Grounding covers the rationales only: the summary feeds no decision and was often meta ("both
  criteria are positive"), which once scored 0.30 and sent a clear case to an analyst. After the change,
  5 of 5 reviews of `t_alex_006` passed (0.85-0.98).
- Temperature 0 is not deterministic here: for a bare "It never came." against a delivered order the
  model scored C4 = -1 in 4 of 5 runs (despite the rubric), so the case skips the question and goes to a
  person. That is the safe direction; a bank would track this rate per criterion.
- A failed Build or Decide step (after its retries) routes the case to an analyst. A staff verdict
  that is neither approve nor deny fails the execution rather than closing the case.
- Not built: Reg E (provisional credit within 10 business days, resolve in 45/90 days) and Reg Z (ack in
  30 days, two billing cycles) case clocks, denial notices, chargebacks to the network. A bank runs
  these clocks in the case system and writes a notice for every denial.
