"""Run the AML triage evaluation for one reviewer.

    uv run python -m evals.aml --reviewer baseline --out evals/aml/results/baseline
    uv run python -m evals.aml --reviewer agents --only LABEL-007,LABEL-002     # a few cases
    uv run python -m evals.aml --variant deep --reviewer agents-graph --repeat 3 \
        --out evals/aml/results/2b/agents-graph                                   # -r1, -r2, -r3
    uv run python -m evals.aml --compare baseline=evals/aml/results/2b/baseline \
        agents-graph=evals/aml/results/2b/agents-graph                          # no AWS
    uv run python -m evals.aml --rescore evals/aml/results/agents                # no AWS

Uses the default AWS credential chain (set AWS_PROFILE to pick a profile). It calls Bedrock
`Converse` only: no other AWS resource is read or changed.
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from agentic_bank.aml import triage
from evals.aml import harness

REGION = "us-east-1"
MODEL_ID = "openai.gpt-oss-120b-1:0"  # the investigator's model (docs/investigator.md)


def load(path: Path) -> dict[str, Any]:
    rows = [json.loads(line) for line in path.with_suffix(".cases.jsonl").open()]
    meta = json.loads(path.with_suffix(".json").read_text())["meta"]
    return {"meta": meta, "metrics": harness.summarize(rows), "rows": rows}


def runs_of(prefix: Path) -> list[dict[str, Any]]:
    paths = sorted(prefix.parent.glob(prefix.name + "-r*.json"))
    return [load(p.with_suffix("")) for p in paths] or [load(prefix)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--reviewer",
        choices=("heuristic", "baseline", "agents", "agents-graph"),
        default="baseline",
    )
    parser.add_argument("--variant", choices=("base", "deep"), default="base")
    parser.add_argument("--model-id", default=MODEL_ID)
    parser.add_argument("--region", default=REGION)
    parser.add_argument("--noise", type=int, default=0, help="also run N unlabelled noise alerts")
    parser.add_argument("--only", help="comma-separated case ids")
    parser.add_argument("--repeat", type=int, default=1, help="runs; with --out, writes -r1, -r2")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out", type=Path, help="write <out>.json, .md and .cases.jsonl")
    parser.add_argument("--rescore", type=Path, help="rebuild the report from <path>.cases.jsonl")
    parser.add_argument("--compare", nargs="+", metavar="NAME=PREFIX", help="table over runs")
    args = parser.parse_args()

    if args.compare:
        results = {}
        for spec in args.compare:
            name, _, prefix = spec.partition("=")
            results[name] = runs_of(Path(prefix))
        print(harness.compare_markdown(results))
        return 0
    if args.rescore:
        print(harness.markdown(load(args.rescore)).split("## Per case")[0])
        return 0

    import boto3
    from botocore.config import Config

    cases, ds, alerts = harness.load_cases(args.noise, args.variant)
    if args.only:
        cases = [c for c in cases if c.id in set(args.only.split(","))]
    config = Config(retries={"mode": "adaptive", "max_attempts": 8}, read_timeout=120)
    runtime = boto3.Session(region_name=args.region).client("bedrock-runtime", config=config)
    graph = args.reviewer in harness.GRAPH_REVIEWERS
    reviewer = triage.select(args.reviewer, (ds, alerts))
    for n in range(1, args.repeat + 1):
        rows = harness.evaluate(
            cases, reviewer, runtime, args.model_id, ds, alerts, args.workers, graph
        )
        meta = {
            "date": date.today().isoformat(),
            "reviewer": args.reviewer,
            "variant": args.variant,
            "run": n,
            "model_id": args.model_id,
            "region": args.region,
            "fingerprint": harness.fingerprint(args.reviewer, args.model_id, args.variant),
        }
        result = {"meta": meta, "metrics": harness.summarize(rows), "rows": rows}
        if args.out:
            out = args.out if args.repeat == 1 else args.out.with_name(f"{args.out.name}-r{n}")
            harness.write_result(result, out)
        print(harness.markdown(result).split("## Prompt-injection")[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
