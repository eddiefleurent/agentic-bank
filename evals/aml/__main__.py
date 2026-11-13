"""Run the AML triage evaluation for one reviewer.

    uv run python -m evals.aml --reviewer baseline --out evals/aml/results/baseline
    uv run python -m evals.aml --reviewer agents --out evals/aml/results/agents
    uv run python -m evals.aml --reviewer agents --only LABEL-007,LABEL-002   # a few cases
    uv run python -m evals.aml --rescore evals/aml/results/agents             # no AWS

Uses the default AWS credential chain (set AWS_PROFILE to pick a profile). It calls Bedrock
`Converse` only: no other AWS resource is read or changed.
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from agentic_bank.aml import triage
from evals.aml import harness

REGION = "us-east-1"
MODEL_ID = "openai.gpt-oss-120b-1:0"  # the investigator's model (docs/investigator.md)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--reviewer", choices=("baseline", "agents"), default="baseline")
    parser.add_argument("--model-id", default=MODEL_ID)
    parser.add_argument("--region", default=REGION)
    parser.add_argument("--noise", type=int, default=0, help="also run N unlabelled noise alerts")
    parser.add_argument("--only", help="comma-separated case ids")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out", type=Path, help="write <out>.json, .md and .cases.jsonl")
    parser.add_argument("--rescore", type=Path, help="rebuild the report from <path>.cases.jsonl")
    args = parser.parse_args()

    if args.rescore:
        rows = [json.loads(line) for line in args.rescore.with_suffix(".cases.jsonl").open()]
        meta = json.loads(args.rescore.with_suffix(".json").read_text())["meta"]
    else:
        import boto3
        from botocore.config import Config

        cases, ds, alerts = harness.load_cases(args.noise)
        if args.only:
            cases = [c for c in cases if c.id in set(args.only.split(","))]
        config = Config(retries={"mode": "adaptive", "max_attempts": 8}, read_timeout=120)
        runtime = boto3.Session(region_name=args.region).client("bedrock-runtime", config=config)
        reviewer = triage.select(args.reviewer)
        rows = harness.evaluate(
            cases, reviewer, runtime, args.model_id, ds, alerts, workers=args.workers
        )
        meta = {
            "date": date.today().isoformat(),
            "reviewer": args.reviewer,
            "model_id": args.model_id,
            "region": args.region,
            "fingerprint": harness.fingerprint(args.reviewer, args.model_id),
        }
    result = {"meta": meta, "metrics": harness.summarize(rows), "rows": rows}
    if args.out:
        harness.write_result(result, args.out)
    print(harness.markdown(result).split("## Per case")[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
