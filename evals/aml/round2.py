"""Round 2 scoring: the single call and overturn rules A, B, C, from the same two-stage runs.

    uv run python -m evals.aml.round2 evals/aml/results/round2/deep2-two_stage

Stage 1 is the single call, so its outcome is read from each row. Rules are pure functions over
the stored raw stage-2 result (`agentic_bank.aml.two_stage.RULES`); nothing here calls a model.
"""

import json
import sys
from pathlib import Path
from typing import Any

from agentic_bank.aml.two_stage import RULES

DEEP = ("hops", "window", "blind_deep")
Row = dict[str, Any]


def load_runs(prefix: Path) -> list[list[Row]]:
    runs, n = [], 1
    while (path := prefix.with_name(f"{prefix.name}-r{n}.cases.jsonl")).exists():
        runs.append([json.loads(line) for line in path.read_text().splitlines()])
        n += 1
    return runs


def single_call(row: Row) -> str:
    """The single call alone: a stage-1 failure escalates, as in every reviewer."""
    return "escalate" if row["stage1_failure"] else row["stage1_recommendation"]


def with_rule(row: Row, rule: str) -> str:
    """The final recommendation if overturn rule `rule` had been used."""
    if single_call(row) == "escalate":
        return "escalate"
    raw = row["stage2_raw"]
    return "escalate" if raw is not None and RULES[rule](raw) else "close"


def reviewers() -> dict[str, Any]:
    return {"single": single_call, **{r: (lambda row, r=r: with_rule(row, r)) for r in RULES}}


def measures(rows: list[Row], decide: Any) -> dict[str, float | None]:
    def rate(sel: Any) -> float | None:
        picked = [r for r in rows if sel(r)]
        if not picked:
            return None  # group not run in this run (shallow suspicious: runs 1 and 2 only)
        return sum(decide(r) == "escalate" for r in picked) / len(picked)

    return {
        "deep_recall": rate(lambda r: r["verdict"] == "suspicious" and r["depth"] in DEEP),
        "shallow_benign_fe": rate(lambda r: r["verdict"] == "benign" and r["depth"] == "shallow"),
        "deep_benign_fe": rate(lambda r: r["verdict"] == "benign" and r["depth"] in DEEP),
        "blind_deep_recall": rate(
            lambda r: r["verdict"] == "suspicious" and r["depth"] == "blind_deep"
        ),
        "shallow_suspicious_recall": rate(
            lambda r: r["verdict"] == "suspicious" and r["depth"] == "shallow"
        ),
        "all_fe": rate(lambda r: r["verdict"] == "benign"),
    }


def noise_band(values: list[float]) -> float:
    """Spread (max minus min) of the single call's per-run values."""
    return max(values) - min(values)


def flip_rate(runs: list[list[Row]]) -> dict[str, float]:
    """Share of cases whose single-call outcome changes across runs."""
    outcomes: dict[str, set[str]] = {}
    for run in runs:
        for r in run:
            outcomes.setdefault(r["id"], set()).add(single_call(r))
    flips = {k for k, v in outcomes.items() if len(v) > 1}
    deep = {r["id"] for r in runs[0] if r["depth"] in DEEP}
    return {
        "all": len(flips) / len(outcomes),
        "deep": len(flips & deep) / len(deep),
        "flipped": len(flips),
        "cases": len(outcomes),
    }


def report(prefix: Path) -> dict[str, Any]:
    runs = load_runs(prefix)
    table: dict[str, list[dict[str, float | None]]] = {
        name: [measures(run, fn) for run in runs] for name, fn in reviewers().items()
    }

    def values(ms: list[dict[str, float | None]], k: str) -> list[float]:
        return [v for m in ms if (v := m[k]) is not None]

    mean = {
        n: {k: sum(values(ms, k)) / len(values(ms, k)) for k in ms[0]} for n, ms in table.items()
    }
    band = {k: noise_band(values(table["single"], k)) for k in table["single"][0]}
    return {"runs": table, "mean": mean, "noise_band": band, "flip": flip_rate(runs)}


if __name__ == "__main__":
    print(json.dumps(report(Path(sys.argv[1])), indent=1))
