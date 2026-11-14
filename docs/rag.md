# Policy assistant: fee and policy answers with citations (RAG)

Status: **implemented, on by default** (`var.knowledge_base = true` since `0c8a16d`).

- **Implemented.** Terraform creates the Knowledge Base `agentic-bank-policies`, the docs bucket, the S3 Vectors index, `ask_policy` and Cedar `read_ask_policy`, unless `knowledge_base` is set false. Bootstrap in that commit lets the apply role manage the docs bucket, the vector bucket, Knowledge Bases and data sources, and pass a role to `bedrock.amazonaws.com`. The workload boundary allows `bedrock:Retrieve` plus the KB role's S3 and S3 Vectors calls. The deploy workflow starts ingestion after an apply that changes something. `scripts/rag_eval.py --kb <id>` runs this eval against that index.
- **Locally tested** (2026-11-12). The same answer code over an in-memory index, with real Titan v2 embeddings, `openai.gpt-oss-120b-1:0` and the deployed grounding guardrail. The table below is that measurement.
- **Not AWS-verified.** No proof script, and no `--kb` eval is committed.

## What it does

A customer asks their agent "What does a domestic wire cost?". The agent calls `bank___ask_policy`
and gets an answer with citations, or "I don't know":

```json
{"answer": "An outgoing domestic wire costs $25.00.",
 "citations": [{"document": "Consumer Fee Schedule", "doc_id": "fee-schedule",
                "section": "Wire transfer fees", "effective_date": "2026-01-01"}],
 "answered": true, "grounding": {"grounding": 1.0, "relevance": 1.0, "passed": true}}
```

```text
knowledge/policies/*.md  --Terraform splits on "## "-->  S3 (one object per section + metadata sidecar)
   --ingestion job--> Titan Text Embeddings v2 --> S3 Vectors index        (managed by Bedrock)
agent -> Gateway (JWT, Cedar: read scope) -> interceptor (customer = token subject)
   -> tools Lambda: Retrieve(question, filter audience = "customer")      (filter set by our code)
   -> score >= MIN_SCORE? -> gpt-oss-120b answers from the passages, cites [n] -> cited ids valid?
   -> guardrail grounding(answer vs cited sections) passed? -> answer + citations, else "I don't know"
```

| Piece | Where |
| --- | --- |
| Corpus: 12 synthetic documents, 51 sections, 9 `customer` and 3 `internal` | `knowledge/policies/`, labels in `knowledge/documents.json` |
| Answer logic, audience filter, refusals, `lookup_policy` | `src/agentic_bank/knowledge/policy.py` |
| Knowledge Base and in-memory retrievers | `src/agentic_bank/knowledge/retrievers.py` |
| Entry points wired to the Lambda environment | `src/agentic_bank/knowledge/service.py` |
| Buckets, vector index, KB, data source, KB role, tools grants | `infra/app/knowledge.tf` |
| Tool schema `ask_policy` (one argument: `question`); Cedar `read_ask_policy` (`read` scope) | `infra/app/gateway.tf`, `infra/app/policy.tf` |
| Eval | `scripts/rag_eval.py`, `knowledge/eval/questions.json` |

## Four ways to say "I don't know"

Each layer catches something the others miss:

1. **Score below `MIN_SCORE`**: off-topic questions ("mortgage rates?") refuse with no model call.
   Saves cost and keeps unrelated text away from the model.
2. **The model returns `{"answer": null}`**: the passages are on topic but do not answer. The eval's
   fee-waiver question scores 0.53 against the customer overdraft policy; only this layer stops it.
3. **The reply is not the schema, or cites a passage it was not given**: no answer without a valid
   citation, so every citation names a section the model actually saw.
4. **The grounding guardrail fails** (the dispute reviewer's guardrail, same thresholds): the only
   check that the answer's words are supported by the cited sections. It costs false refusals (below).

## Why the audience filter is set server-side

Customers may read only `audience = customer` documents. Internal ones (fee waiver limits, fraud
escalation thresholds) would help someone game the bank. The filter comes from **who is calling**,
never from the request:

- Every gateway caller is a customer token. `ask_policy` takes no audience parameter; it always
  uses `customer`. The tool schema has no audience field and the handler ignores one if sent
  (`tests/test_knowledge.py`).
- A filter the model chooses is a filter a prompt injection chooses ("search internal documents").
  Here no text the model writes reaches the filter.
- Defense in depth: after Retrieve, any passage whose `audience` is not allowed, or is missing, is
  dropped again (fail closed).
- Cedar cannot do this job: it sees the tool call, not which documents come back.

**Residual risk:** the filter is our code in the tools Lambda, not IAM. A bug or a compromised tools
process could omit it. A bank puts internal documents in a **separate Knowledge Base** that the tools
role has no `bedrock:Retrieve` on, so AWS denies the read whatever the code does (the same idea as
the per-customer `LeadingKeys` session policy). On S3 Vectors that costs nothing extra; it is the
first upgrade once internal documents matter.

`lookup_policy(query)` in `service.py` is the bank-side seam for a future dispute investigator: it
reads `customer` **and** `internal` documents and returns passages with citations (no generated
answer, so the calling agent cites them). It is not a gateway tool. The investigator uses it when `INVESTIGATOR_POLICY=knowledge_base`
([investigator.md](investigator.md#the-policy-seam)); the default stays the local rules file.

## S3 Vectors vs OpenSearch Serverless

| | S3 Vectors (chosen) | OpenSearch Serverless |
| --- | --- | --- |
| Idle cost | Storage only: under 1 MB here, about $0.00/month | About $175/month minimum (2 x 0.5 OCU, redundancy off, $0.24 per OCU-hour) |
| Per query | Small request charge plus data scanned | Included in the running capacity |
| Latency | Sub-second (around 100 ms warm) | Tens of milliseconds |
| Features | Vector similarity with metadata filters | Also hybrid keyword + vector search |

At a few hundred sections and a few queries a minute, S3 Vectors costs nothing idle and its latency
is small next to the model call (1 to 2 s). OpenSearch earns its fixed cost at high volume, strict
latency targets, or when keyword search matters (exact fee names, form numbers). List prices as
understood on 2026-11-12.

## Chunking: one section is one chunk (`chunking_strategy = NONE`)

Terraform splits each document on `## ` headings and uploads one object per section with a
`.metadata.json` sidecar (`doc_id`, `title`, `section`, `audience`, `effective_date`). Each section
is prefixed with its document title and effective date, so a short section still says where it is
from.

- **Citations are exact**: the chunk is the section, so the citation is its heading.
- **Rules are not cut in half**: "$29 per item, at most three per day, no fee if overdrawn by $50
  or less" stays one chunk; fixed-size chunking could split it.
- Sections are short (19 to 105 words). Hierarchical and semantic chunking add cost for no gain here.
- The cost: authors must write in sections. Python (`corpus.split_sections`) and Terraform mirror
  the same split by hand; a test checks the section text template matches, not the split itself.

**Embeddings: Titan Text Embeddings v2, 1024 dimensions, normalized.** On-demand, supported by
Knowledge Bases on S3 Vectors, embedding the whole corpus costs well under a cent.

## Retrieval eval vs answer eval

- **Retrieval eval**: did the right document and section come back in the top k for the caller's
  audience? No model, deterministic, cheap. Says nothing about the answer.
- **Answer eval**: did it answer when it should, cite the right section, refuse when it should?
  Uses the model, varies run to run. Correctness would need reference answers and a judge; we only
  measure the structural part.

The answerer stays on `openai.gpt-oss-120b-1:0`. A trial on Nemotron 3 Super (the dispute classifier's model) answered only 11/17 (the grounding check refused 6 on relevance), so it was not adopted; the answer format itself parsed fine.

Local mode, 20 questions (17 answerable, 2 internal-only, 1 not in the corpus):

| Metric | Result |
| --- | --- |
| Document hit@1 / @5 | 17/17, 17/17 |
| Section hit@1 / @3 | 16/17, 17/17 (q06: "60 days" sits in two sections) |
| Internal passages returned under the customer filter | 0 |
| Answered (of 17 answerable), two runs | 16/17, then 15/17 |
| Answered with the expected document and section cited | 100% of answered |
| Must-refuse questions refused | 3/3, both runs |

What the numbers hide:

- **False refusals come from the relevance check.** q01 and q12 were refused in a local run that was not committed. See [Known issues](#known-issues).
- **A cited, grounded answer can still be wrong.** For "I approved an ACH at 8 pm, when will it
  arrive?" the model said "the next business day"; the policy implies the day after. Right section,
  passed grounding. Only a correctness eval catches this.

Run it: `uv run python scripts/rag_eval.py` (about $0.05) or `--retrieval-only` (under $0.01).

## How deploy turns it on

The permissions, the default and the re-index step are in the tree. A live apply is not recorded here as AWS-verified.

1. Bootstrap permissions are in `infra/bootstrap` (`0c8a16d`): the apply role may manage `agentic-bank-kb-docs-*`, `agentic-bank-kb-*`, Knowledge Bases and data sources, and pass a role to `bedrock.amazonaws.com`. The workload boundary allows `bedrock:Retrieve` on `knowledge-base/*`, and for the KB role `s3:GetObject` and `s3:ListBucket` on the docs bucket plus `s3vectors:GetIndex`, `QueryVectors`, `PutVectors`, `GetVectors` and `DeleteVectors` on the vector index.
2. `knowledge_base` defaults to true. Set it false to leave the tool and the Knowledge Base out of an apply.
3. Ingestion is not a Terraform resource. The deploy workflow starts it and waits when the apply had changes. By hand: `aws bedrock-agent start-ingestion-job` with the `knowledge_base_id` and `knowledge_data_source_id` outputs. Re-run after any document change.
4. `MIN_SCORE` is still 0.30, raw cosine from local mode. Retrieve on S3 Vectors uses its own scale. Recalibrating it takes a `--kb` run, and none is committed.

Expected cost: idle about $0.00/month; about $0.001 per question (query embedding, Retrieve,
roughly 2,000 model tokens, one grounding check).

## Known issues

q01 and q12 are answerable questions the relevance check refused. Those refusals were observed in a local run that was not committed; no file under `evals/` records the scores. The floor is the dispute reviewer's (`RELEVANCE_MIN` 0.7, grounding 0.65). The questions and the sections they should hit use different words. Evals are frozen: either fix needs a new `uv run python scripts/rag_eval.py` (about $0.05) before a committed score can replace that observation.

**q01.** "How much does it cost to send a wire to another US bank?" Expected: `fee-schedule` / "Wire transfer fees" (an outgoing domestic wire is $25.00). Refused in that uncommitted local run. The question says "another US bank"; the section says "domestic" and never "another US bank". Fix idea: say that link in the section ("a wire to another US bank is a domestic wire"). Deferred.

**q12.** "I lost my debit card. How fast can I get a new one and what does it cost?" Expected: `card-replacement` / "Replacement timing and cost" (free, 7 to 10 business days; expedited $25.00 in 2 business days). Refused in that same uncommitted local run. That section says "replacement" and "expedited delivery"; it never says "lost". "Lost" and the new card number are in "Lost or stolen card", which has no price or timing. Same paraphrase gap as q01. Fix idea: restate the lost-card wording in the timing section. Deferred.

## Known limits

- Not AWS-verified: no proof covers the Knowledge Base's metadata, ingestion or Retrieve scores.
- Audience isolation is our code, not IAM (above). Both buckets use AWS-managed encryption; a bank
  uses a customer-managed key per data class.
- Worst case, three Bedrock calls with one retry each can exceed the tools Lambda's 30 s timeout.
- No document versioning: `effective_date` is cited but superseded documents are not hidden.
- 20 hand-written questions on a corpus by the same author: the mechanism works, not that recall
  holds on real customer phrasing. Answers are checked for grounding and citations, not correctness.
- No per-customer rate limit on a tool that calls a model.
