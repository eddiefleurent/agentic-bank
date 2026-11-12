"""Run the dispute reviewer evaluation.

    uv run python -m evals.disputes --out evals/disputes/results/baseline   # all cases, ~$0.10
    uv run python -m evals.disputes --sample 20 --gate                      # cheap smoke run
    uv run python -m evals.disputes --rescore evals/disputes/results/baseline --gate   # no AWS
    uv run python -m evals.disputes --cases evals/disputes/heldout.jsonl \
        --out evals/disputes/results/heldout                                    # 16 held-out

Uses the default AWS credential chain (set AWS_PROFILE to pick a profile). It calls Bedrock
`Converse` and `ApplyGuardrail` only, plus one `ListGuardrails` to find the guardrail by name.
"""

import argparse
import importlib
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from evals.disputes import harness

REGION = "us-east-1"
MODEL_ID = "nvidia.nemotron-super-3-120b"
GUARDRAIL_NAME = "agentic-bank-grounding"
DEFAULT_REVIEWER = "agentic_bank.disputes.review:run_review"


def load_reviewer(spec: str) -> harness.Reviewer:
    module, _, name = spec.partition(":")
    return getattr(importlib.import_module(module), name)


def find_guardrail(session: Any, name: str) -> tuple[str, str]:
    """The guardrail's id and its newest numbered version (what the review Lambda uses)."""
    bedrock = session.client("bedrock")
    found = [g for g in bedrock.list_guardrails()["guardrails"] if g["name"] == name]
    if not found:
        raise SystemExit(f"no guardrail named {name!r} in this account and region")
    versions = bedrock.list_guardrails(guardrailIdentifier=found[0]["id"])["guardrails"]
    numbered = [int(v["version"]) for v in versions if v["version"].isdigit()]
    return found[0]["id"], str(max(numbered)) if numbered else "DRAFT"


def console_tracing() -> None:
    """Print spans to stderr. Needs the dev dependency opentelemetry-sdk."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter(out=sys.stderr)))
    trace.set_tracer_provider(provider)


def run(args: argparse.Namespace, cases: list[harness.Case]) -> dict[str, Any]:
    import boto3
    from botocore.config import Config

    session = boto3.Session(region_name=args.region)
    guardrail_id, version = args.guardrail_id, args.guardrail_version
    if not guardrail_id:
        guardrail_id, version = find_guardrail(session, args.guardrail_name)
    config = Config(retries={"mode": "adaptive", "max_attempts": 8}, read_timeout=120)
    runtime = session.client("bedrock-runtime", config=config)
    picked = harness.stratified_sample(cases, args.sample, args.seed) if args.sample else cases
    rows = harness.evaluate(
        picked,
        load_reviewer(args.reviewer),
        runtime,
        args.model_id,
        guardrail_id,
        version,
        workers=args.workers,
    )
    meta = {
        "label": args.label or ("full run" if not args.sample else f"sample of {len(picked)}"),
        "date": date.today().isoformat(),
        "reviewer": args.reviewer,
        "model_id": args.model_id,
        "region": args.region,
        "guardrail": f"{args.guardrail_name} v{version}",
        "dataset_size": len(cases),
        "sample_seed": args.seed if args.sample else None,
        "dataset": args.cases.name,
        "fingerprint": harness.fingerprint(args.model_id, args.cases),
    }
    return harness.build_result(rows, cases, meta)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--sample", type=int, help="run about N cases, stratified by category")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--gate", action="store_true", help="exit 1 if a bound is crossed")
    parser.add_argument("--out", type=Path, help="write <out>.json, .md and .cases.jsonl")
    parser.add_argument("--rescore", type=Path, help="rebuild the report from <path>.cases.jsonl")
    parser.add_argument("--cases", type=Path, default=harness.CASES, help="dataset (JSON lines)")
    parser.add_argument("--reviewer", default=DEFAULT_REVIEWER, help="module:callable")
    parser.add_argument("--label")
    parser.add_argument("--model-id", default=MODEL_ID)
    parser.add_argument("--region", default=REGION)
    parser.add_argument("--guardrail-name", default=GUARDRAIL_NAME)
    parser.add_argument("--guardrail-id", help="skip the lookup by name")
    parser.add_argument("--guardrail-version", default="DRAFT")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--trace-console", action="store_true", help="print OpenTelemetry spans")
    args = parser.parse_args()

    cases = harness.load_cases(args.cases)
    if args.trace_console:
        console_tracing()
    if args.rescore:
        rows = [json.loads(line) for line in args.rescore.with_suffix(".cases.jsonl").open()]
        meta = json.loads(args.rescore.with_suffix(".json").read_text())["meta"]
        result = harness.build_result(rows, cases, meta)
    else:
        result = run(args, cases)
    if args.out:
        harness.write_result(result, args.out)
    report = harness.markdown(result)
    print(report.split("## Confusion")[0] if args.out else report)
    sampled = result["metrics"]["n"] < len(cases)
    bounds = json.loads(harness.BOUNDS.read_text())
    problems = harness.check_gate(result["metrics"], bounds, sampled)
    for problem in problems:
        print(f"GATE FAIL: {problem}", file=sys.stderr)
    if args.gate and not problems:
        print("GATE PASS")
    return 1 if args.gate and problems else 0


if __name__ == "__main__":
    sys.exit(main())
