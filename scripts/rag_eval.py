"""Retrieval eval for the policy assistant, in local mode: the real tool logic over an in-memory
index of the same sections, real Titan v2 embeddings, the real model and grounding guardrail.

    uv run python scripts/rag_eval.py                  # retrieval + answers (about $0.05)
    uv run python scripts/rag_eval.py --retrieval-only # embeddings only (well under $0.01)

Needs AWS credentials that may call Bedrock in us-east-1. Answers need the deployed guardrail
`agentic-bank-grounding` (found by name; or set GUARDRAIL_ID and GUARDRAIL_VERSION).

Two evals, measured separately (docs/rag.md):
  retrieval  did the right document and section come back in the top k, for the caller's audience?
  answers    did the tool answer when it should, cite the right section, and refuse when it should?
"""

import argparse
import json
import os
import sys
from typing import Any

import boto3

from agentic_bank.knowledge import policy
from agentic_bank.knowledge.corpus import CORPUS_DIR, load_corpus
from agentic_bank.knowledge.retrievers import InMemoryRetriever, titan_embedder

REGION = "us-east-1"
MODEL_ID = "openai.gpt-oss-120b-1:0"  # the policy answerer stays on gpt-oss (see docs/rag.md)
KS = (1, 3, 5)


def rank_of(passages: list[policy.Passage], doc_id: str, section: str | None = None) -> int | None:
    for i, p in enumerate(passages, start=1):
        if p.doc_id == doc_id and (section is None or p.section == section):
            return i
    return None


def guardrail() -> tuple[str, str]:
    if os.environ.get("GUARDRAIL_ID"):
        return os.environ["GUARDRAIL_ID"], os.environ.get("GUARDRAIL_VERSION", "1")
    listed = boto3.client("bedrock", region_name=REGION).list_guardrails()["guardrails"]
    match = [g["id"] for g in listed if g["name"] == "agentic-bank-grounding"]
    if not match:
        sys.exit("guardrail agentic-bank-grounding not found: set GUARDRAIL_ID or --retrieval-only")
    return match[0], os.environ.get("GUARDRAIL_VERSION", "1")


def pct(n: int, d: int) -> str:
    return f"{n}/{d} ({100 * n / d:.0f}%)" if d else "n/a"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--retrieval-only", action="store_true")
    args = parser.parse_args()

    questions = json.loads((CORPUS_DIR / "eval" / "questions.json").read_text())
    bedrock = boto3.client("bedrock-runtime", region_name=REGION)
    sections = load_corpus()
    retriever = InMemoryRetriever(sections, titan_embedder(bedrock))
    print(f"indexed {len(sections)} sections from {len({s.doc_id for s in sections})} documents")

    answerable = [q for q in questions if "refuse" not in q]
    rows: list[dict[str, Any]] = []
    doc_hits = dict.fromkeys(KS, 0)
    sec_hits = dict.fromkeys(KS, 0)
    leaks = 0
    for q in questions:
        hits = retriever.retrieve(q["question"], policy.CUSTOMER, max(KS))
        leaks += sum(p.audience != "customer" for p in hits)
        row: dict[str, Any] = {
            "id": q["id"],
            "top_score": round(hits[0].score, 3) if hits else None,
            "top": [f"{p.doc_id} / {p.section}" for p in hits[:3]],
        }
        if "refuse" not in q:
            d, s = rank_of(hits, q["doc_id"]), rank_of(hits, q["doc_id"], q["section"])
            row |= {"doc_rank": d, "section_rank": s}
            for k in KS:
                doc_hits[k] += d is not None and d <= k
                sec_hits[k] += s is not None and s <= k
        elif q["refuse"] == "internal":
            staff = retriever.retrieve(q["question"], policy.STAFF, max(KS))
            row["staff_section_rank"] = rank_of(staff, q["doc_id"], q["section"])
        rows.append(row)

    n = len(answerable)
    print("\nRETRIEVAL (customer filter, k = 1/3/5)")
    print("  doc hit@k:     " + "  ".join(f"@{k} {pct(doc_hits[k], n)}" for k in KS))
    print("  section hit@k: " + "  ".join(f"@{k} {pct(sec_hits[k], n)}" for k in KS))
    print(f"  internal passages returned to the customer filter: {leaks}")
    for row, q in zip(rows, questions, strict=True):
        if q.get("refuse") == "internal":
            rank = row["staff_section_rank"]
            print(f"  {q['id']} internal doc rank with the staff filter: {rank}")
    pairs = list(zip(rows, questions, strict=True))
    ans_scores = sorted(r["top_score"] for r, q in pairs if "refuse" not in q)
    ref_scores = sorted(r["top_score"] for r, q in pairs if "refuse" in q)
    median = ans_scores[len(ans_scores) // 2]
    print(f"  top score, answerable: min {ans_scores[0]}  median {median}")
    print(f"  top score, must-refuse: {ref_scores}   MIN_SCORE = {policy.MIN_SCORE}")
    for row in rows:
        if row.get("section_rank") != 1 and "doc_rank" in row:
            print(f"  miss@1 {row['id']}: got {row['top'][0]} (section rank {row['section_rank']})")

    if not args.retrieval_only:
        gid, version = guardrail()
        answerer = policy.bedrock_answerer(bedrock, MODEL_ID)
        grounder = policy.guardrail_grounder(bedrock, gid, version)
        answered = cited_doc = cited_section = refused_ok = 0
        cites = cites_right = 0
        for row, q in zip(rows, questions, strict=True):
            out = policy.ask_policy(q["question"], retriever, answerer, grounder)
            row["answer"] = out
            if "refuse" in q:
                refused_ok += not out["answered"]
                continue
            if not out["answered"]:
                print(f"  refused answerable {q['id']}: {out['reason']} {out.get('grounding')}")
                continue
            answered += 1
            docs = [c["doc_id"] for c in out["citations"]]
            pairs = [(c["doc_id"], c["section"]) for c in out["citations"]]
            cited_doc += q["doc_id"] in docs
            cited_section += (q["doc_id"], q["section"]) in pairs
            cites += len(docs)
            cites_right += docs.count(q["doc_id"])
        print("\nANSWERS (ask_policy, customer audience)")
        print(f"  answered (of answerable):        {pct(answered, n)}")
        print(f"  cites the expected document:     {pct(cited_doc, answered)} of answered")
        print(f"  cites the expected section:      {pct(cited_section, answered)} of answered")
        print(f"  citation precision (right doc):  {pct(cites_right, cites)}")
        print(f"  must-refuse questions refused:   {pct(refused_ok, len(questions) - n)}")
        for row, q in zip(rows, questions, strict=True):
            if "refuse" in q:
                print(f"  {q['id']} ({q['refuse']}): {row['answer'].get('reason', 'ANSWERED')}")


if __name__ == "__main__":
    main()
