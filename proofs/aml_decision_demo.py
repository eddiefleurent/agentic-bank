"""Local demo: a decision-model second opinion on synthetic AML cases.

uv run python proofs/aml_decision_demo.py
uv run python proofs/aml_decision_demo.py --with-bedrock
uv run python proofs/aml_decision_demo.py --all
uv run python proofs/aml_decision_demo.py --all --model nace-ai/drex-max-finance
"""

import argparse
import json
import os
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from agentic_bank.aml.decision import (  # noqa: E402
    MODEL,
    DecisionHTTPError,
    model_slug,
    second_opinion,
    urlopen_transport,
)
from agentic_bank.aml.snapshot import build_case_snapshot  # noqa: E402
from evals.aml import harness  # noqa: E402

DEMO_LIMIT = 10
BEDROCK_MODEL = "openai.gpt-oss-120b-1:0"
RESULTS = ROOT / "evals" / "aml" / "results"
STORED = {
    "base": RESULTS / "baseline.cases.jsonl",
    "deep": RESULTS / "two-stage" / "deep-baseline-r3.cases.jsonl",
}


@dataclass(frozen=True)
class Case:
    case_id: str
    variant: str
    verdict: str
    depth: str
    alert_id: str


def load_cases(limit: int | None = None) -> list[tuple[Case, dict]]:
    pending = []
    for variant in ("base", "deep"):
        cases, ds, alerts = harness.load_cases(variant=variant)
        for case in cases:
            pending.append(
                (
                    Case(case.id, variant, case.verdict, case.depth, case.alert.alert_id),
                    case,
                    ds,
                    alerts,
                )
            )
    cases, ds, alerts = harness.load_deep2()
    for case in cases:
        if case.depth == "blind_deep":
            pending.append(
                (
                    Case(case.id, "deep2", case.verdict, case.depth, case.alert.alert_id),
                    case,
                    ds,
                    alerts,
                )
            )
    if limit is not None:
        pending = _demo_slice(pending, limit)
    return [(ref, build_case_snapshot(case.alert, ds, alerts)) for ref, case, ds, alerts in pending]


def _demo_slice(pending: list, limit: int) -> list:
    def take(pred, n: int) -> list:
        return [row for row in pending if pred(row[0])][:n]

    chosen = (
        take(lambda c: c.variant == "base" and c.verdict == "suspicious", 4)
        + take(lambda c: c.variant == "base" and c.verdict == "benign", 3)
        + take(lambda c: c.variant == "deep" and c.depth != "shallow", 3)
    )
    return chosen[:limit]


def run_cases(
    snapshots: list[dict],
    model: str,
    *,
    workers: int = 1,
    budget: float | None = None,
    abort_on_4xx: bool = False,
    progress: bool = False,
):
    """One loop for the 10-case demo and the full run. `model` selects Reflex or Drex."""
    n = len(snapshots)
    opinions: list = [None] * n
    latencies: list = [None] * n
    spent = 0.0
    budget_hit = False
    fatal = None
    hold_for_first = abort_on_4xx
    tag = model_slug(model)
    nxt = 0
    pending: dict = {}
    finished = 0

    def call(snapshot: dict):
        seen: list[DecisionHTTPError] = []

        def transport(body: bytes, headers: dict[str, str], timeout: float) -> bytes:
            try:
                return urlopen_transport(body, headers, timeout)
            except DecisionHTTPError as exc:
                seen.append(exc)
                raise

        started = time.perf_counter()
        opinion = second_opinion(
            snapshot, model=model, transport=transport if abort_on_4xx else None
        )
        err = None
        if abort_on_4xx and opinion is None and seen and 400 <= seen[-1].status < 500:
            err = seen[-1]
        return opinion, time.perf_counter() - started, err

    with ThreadPoolExecutor(max_workers=workers) as pool:
        while pending or nxt < n:
            while not budget_hit and fatal is None and len(pending) < workers and nxt < n:
                if hold_for_first and nxt > 0:
                    break
                pending[pool.submit(call, snapshots[nxt])] = nxt
                nxt += 1
            if not pending:
                break
            done, _ = wait(set(pending), return_when=FIRST_COMPLETED)
            for fut in done:
                index = pending.pop(fut)
                opinion, latency, err = fut.result()
                opinions[index] = opinion
                latencies[index] = latency
                if opinion is not None and opinion.cost_usd:
                    spent += opinion.cost_usd
                    if budget is not None and spent > budget:
                        budget_hit = True
                if index == 0:
                    hold_for_first = False
                    fatal = err
                finished += 1
                if (
                    fatal is None
                    and progress
                    and (finished == 1 or finished % 10 == 0 or finished == n)
                ):
                    print(f"{tag} {finished}/{n} ${spent:.4f}", file=sys.stderr)
            if fatal is not None:
                break
    if budget_hit and budget is not None:
        print(f"aborted: reported cost ${spent:.4f} exceeds ${budget:.2f}", file=sys.stderr)
    return opinions, spent, budget_hit, latencies, fatal


def _lines(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def stored_rec(rows: list[dict], case_id: str) -> str | None:
    for row in rows:
        rec = row.get("recommendation")
        if row.get("id") == case_id and rec in ("close", "escalate"):
            return rec
    return None


def live_bedrock(snapshot: dict) -> str | None:
    import boto3

    from agentic_bank.aml.triage import run_baseline

    client = boto3.client("bedrock-runtime", region_name="us-east-1")
    try:
        run = run_baseline(client, BEDROCK_MODEL, snapshot)
    except Exception:
        return None
    rec = run.outcome.recommendation
    return rec if rec in ("close", "escalate") else None


def agrees(choice: str | None, primary: str | None) -> str:
    if choice not in ("close", "escalate") or primary not in ("close", "escalate"):
        return "—"
    return "yes" if choice == primary else "no"


def format_table(lines: list[tuple[str, ...]]) -> str:
    header = ("case", "label", "bedrock", "p(escalate)", "band", "agrees")
    rows = [header, *lines]
    widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
    return "\n".join("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AML decision-model second opinion (local demo)")
    parser.add_argument(
        "--with-bedrock",
        action="store_true",
        help="Call the Bedrock baseline live. Default: read evals/aml/results.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Every base and deep case, plus blind-deep. No Bedrock. Abort above $1.",
    )
    parser.add_argument(
        "--model",
        default=MODEL,
        help=f"Pinned decision model id. Default {MODEL}.",
    )
    args = parser.parse_args(argv)
    if args.all:
        from aml_decision_full import run_all

        return run_all(args.model, load_cases, run_cases)
    loaded = load_cases(DEMO_LIMIT)
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("OPENROUTER_API_KEY is not set; no decision call will be made.")
    opinions, _, _, _, _ = run_cases([snap for _, snap in loaded], args.model)
    stored = {variant: _lines(path) for variant, path in STORED.items()}
    table = []
    reported = 0.0
    saw_cost = False
    for (ref, snapshot), opinion in zip(loaded, opinions, strict=True):
        primary = (
            live_bedrock(snapshot)
            if args.with_bedrock
            else stored_rec(stored[ref.variant], ref.case_id)
        )
        label = ref.verdict if ref.variant == "base" else f"{ref.verdict}/{ref.depth}"
        if opinion is None:
            p_text, band, choice = "—", "—", None
        else:
            p_text, band, choice = f"{opinion.p_escalate:.2f}", opinion.band, opinion.choice
            if opinion.cost_usd is not None:
                reported += opinion.cost_usd
                saw_cost = True
        table.append(
            (
                f"{ref.variant}/{ref.case_id}",
                label,
                primary or "—",
                p_text,
                band,
                agrees(choice, primary),
            )
        )
    print(format_table(table))
    cost = f" OpenRouter reported ${reported:.6f}." if saw_cost else ""
    print(f"Decision calls: {len(opinions)} (cap {DEMO_LIMIT}).{cost}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
