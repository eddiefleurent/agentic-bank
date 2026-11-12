# 0009. PII tokenization of the customer's narrative

Status: accepted, deployed

## Context
A dispute statement is free text: customers type addresses, phone numbers and card numbers into it.
It feeds a model, Step Functions history and logs, none of which should hold those values.

## Decision
- The tools Lambda tokenizes `statement` and `answer` right after Cedar, before anything stores them:
  Amazon Comprehend `DetectPiiEntities` (names, addresses, phones, emails, card, bank and ID numbers,
  score >= 0.5) plus a deterministic rule that turns any run of 7+ digits into `<NUMBER_...>`, so card
  digits never depend on a model. Dates are kept (the review needs them): Comprehend's DATE_TIME is
  not tokenized, and a digit run that is exactly a hyphenated date (`2026-11-07`, `11-07-2026`) is
  skipped by the backstop.
- Each value is KMS-encrypted (key `alias/agentic-bank-pii`, context `{case_id, token}`) into the vault
  table `agentic-bank-vault`. Any Comprehend, KMS or vault error refuses the dispute ("try again").
- The key lives in `infra/bootstrap`, applied by a human. Whoever can edit the key policy can grant
  themselves Decrypt. The apply role has no direct `kms:Decrypt` and no `kms:PutKeyPolicy`, so it cannot
  decrypt the vault or rewrite the key policy. It can still replace the analyst Lambda, whose role can
  decrypt, and invoke it. Deployment administrators are trusted with that path in this demo. KMS stops
  other runtime roles; it does not isolate PII from whoever deploys the analyst function. The key policy
  denies `Decrypt`/`ReEncrypt*` to every principal except the analyst function's role, and any use
  without the two context keys.
- Reveal: the analyst function URL (`AWS_IAM`) checks the caller is an `agentic-bank-app-analyst`
  session, the case is in review and the token belongs to it, writes an `AUDIT#` row first, then
  decrypts. CloudTrail records the Decrypt with its context.
- Not tokenized: tool responses. The reader is the customer's own agent reading the customer's data.
  Revisit when a third party reads tool output.

## Consequences
- Historical canary checks in `proofs/analyst_pii.py`: tested case rows and execution history hold tokens; a CloudWatch
  search for each raw canary in every `agentic-bank` log group finds nothing; reveal works for the
  analyst and is audited; admin is refused by the Lambda (403) and by KMS (AccessDenied).
- Comprehend is about $0.0003 per statement and runs in-region; Presidio was rejected (1.3 GB image,
  8 s cold start). A bank also sets the Organizations AI-services opt-out and checks PCI DSS scope.
- The account admin can still edit the key policy or the Lambda code: a bank separates key
  administrators from data users, alarms on `PutKeyPolicy`, and ships audit to a separate account.
- Detection can miss PII, especially names and addresses; the digit rule covers only contiguous digits
  or digits separated by single spaces/hyphens. Canary results do not guarantee zero PII leakage.
- Vault rows have no TTL (dispute records kept); a bank adds a retention job.
