"""Evaluation harness for AML triage: labelled alerts in, recall and false escalations out.

One alert per planted case (50: 25 suspicious, 25 benign look-alikes): the alert that matches the
label (customer, an expected rule, a shared evidence transaction), preferring the rule of the
case's own typology. Optionally a few noise alerts, which have no label and are only counted.

The primary metric is recall on suspicious cases: escalated / 25. A missed suspicious case is the
costly error in AML. Because every failure becomes `escalate`, recall is also shown counting only
escalations the model actually decided, so a reviewer that fails on everything cannot look good.

Two data variants: `base` (the 50 cases, slice 2) and `deep` (the same 50 regenerated, plus 16
designed so the decisive evidence is outside the snapshot; `aml/deep.py`). Rows carry `depth`, and
the summary breaks recall and false escalations down by shallow and deep.

Reviewers `baseline` and `agents` get the snapshot (the evidence pack). `agents-graph` gets only
`{"alert": ...}` and reads the whole dataset through its own tools.

No AWS call is made here except through the `bedrock` client handed to `evaluate`.
"""

import hashlib
import inspect
import json
import time
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentic_bank.aml import data, deep, rules, snapshot, triage
from agentic_bank.aml.case_tools import txn_ids
from agentic_bank.aml.rules import Alert
from evals.disputes.harness import percentile, rate, shorten, table, wilson

Reviewer = Callable[[Any, str, dict[str, Any]], triage.TriageRun]

HERE = Path(__file__).parent
LABELS = {"base": HERE / "labels.json", "deep": HERE / "deep" / "labels.json"}
GRAPH_REVIEWERS = {"agents-graph"}
RULE_OF = {
    "structuring": "R1",
    "rapid_movement": "R2",
    "funnel_account": "R3",
    "high_risk_jurisdiction": "R4",
    "layering": "R5",
}
# USD per million tokens (input, output), Bedrock on-demand, us-east-1, as in
# scripts/compare_reviewers.py (2026-11-12). Change them if you change model, tier or region.
PRICES = {
    "openai.gpt-oss-120b-1:0": (0.15, 0.60),
    "nvidia.nemotron-super-3-120b": (0.15, 0.65),
}


@dataclass(frozen=True)
class EvalCase:
    id: str  # label id, or NOISE-<alert id>
    verdict: str  # suspicious | benign | noise
    typology: str  # label typology, or "none" for noise
    alert: Alert
    injection_txn_ids: list[str] = field(default_factory=list)
    depth: str = "shallow"  # shallow | hops | window
    decisive_txn_ids: list[str] = field(default_factory=list)


def pick_alert(case: dict[str, Any], alerts: list[Alert]) -> Alert:
    hits = [
        a
        for a in alerts
        if a.customer_id in case["customer_ids"]
        and a.rule_id in case["expected_rules"]
        and set(a.txn_ids) & set(case["evidence_txn_ids"])
    ]
    if not hits:
        raise ValueError(f"no alert matches {case['label_id']}")
    evidence = set(case["evidence_txn_ids"])
    hits.sort(
        key=lambda a: (
            a.customer_id != case["primary_customer_id"],
            a.rule_id != RULE_OF[case["typology"]],
            -len(set(a.txn_ids) & evidence),
            a.alert_id,
        )
    )
    return hits[0]


def load_cases(
    noise: int = 0, variant: str = "base"
) -> tuple[list[EvalCase], data.Dataset, list[Alert]]:
    ds, labels = deep.generate() if variant == "deep" else data.generate()
    alerts = rules.run_rules(ds)
    cases = [
        EvalCase(
            c["label_id"],
            c["verdict"],
            c["typology"],
            pick_alert(c, alerts),
            c["injection_txn_ids"],
            c.get("depth", "shallow"),
            c.get("decisive_txn_ids", []),
        )
        for c in labels["cases"]
    ]
    noisy = [a for a in alerts if a.customer_id in set(labels["noise_customer_ids"])]
    cases += [EvalCase(f"NOISE-{a.alert_id}", "noise", "none", a) for a in noisy[:noise]]
    return cases, ds, alerts


def cost_usd(model_id: str, tokens_in: int, tokens_out: int) -> float:
    price_in, price_out = PRICES.get(model_id, (0.0, 0.0))
    return (tokens_in * price_in + tokens_out * price_out) / 1e6


def evaluate_case(
    case: EvalCase,
    reviewer: Reviewer,
    bedrock: Any,
    model_id: str,
    ds: data.Dataset,
    alerts: list[Alert],
    graph: bool = False,
) -> dict[str, Any]:
    snap = snapshot.build_case_snapshot(case.alert, ds, alerts)
    given = {"alert": snap["alert"]} if graph else snap  # graph agents start from the alert alone
    start = time.perf_counter()
    try:
        run = reviewer(bedrock, model_id, given)
    except Exception as exc:  # reviewers never raise; if one does, a person reviews the alert
        run = triage.TriageRun(outcome=triage.failed(f"reviewer raised {type(exc).__name__}"))
    latency = time.perf_counter() - start
    out = run.outcome
    advice = out.advice or {}
    tokens_in, tokens_out = run.usage.get("inputTokens", 0), run.usage.get("outputTokens", 0)
    decisive = set(case.decisive_txn_ids)
    read = set(run.audit.get("read_ids", []))
    return {
        "id": case.id,
        "alert_id": case.alert.alert_id,
        "rule_id": case.alert.rule_id,
        "verdict": case.verdict,
        "typology": case.typology,
        "injection_txn_ids": case.injection_txn_ids,
        "depth": case.depth,
        "decisive_in_pack": bool(decisive) and decisive <= txn_ids(snap),
        "decisive_read": (bool(decisive) and decisive <= read) if graph else None,
        "graph_rows_read": run.audit.get("rows_read"),
        "graph_calls": run.audit.get("calls"),
        "recommendation": out.recommendation,
        "failure": out.failure,
        "predicted_typology": advice.get("typology"),
        "model_recommendation": advice.get("recommendation"),
        "flagged_instruction_memos": advice.get("instruction_memo_txn_ids", []),
        "latency_s": round(latency, 2),
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "model_calls": run.usage.get("model_calls", 0),
        "tool_calls": len(run.tool_calls),
        "refused_tools": run.refused_tools,
        "stop_reason": run.stop_reason,
        "cost_usd": round(cost_usd(model_id, tokens_in, tokens_out), 6),
        "advice": out.advice,
    }


def evaluate(
    cases: Iterable[EvalCase],
    reviewer: Reviewer,
    bedrock: Any,
    model_id: str,
    ds: data.Dataset,
    alerts: list[Alert],
    workers: int = 4,
    graph: bool = False,
) -> list[dict[str, Any]]:
    def one(case: EvalCase) -> dict[str, Any]:
        return evaluate_case(case, reviewer, bedrock, model_id, ds, alerts, graph)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, cases))


# ---------------------------------------------------------------------------- metrics

CITATION_FAILURES = ("cited ids not in case", "cited fields not read or empty")


def share(rows: list[dict[str, Any]], typology: str, recommendation: str) -> str:
    """ "k/n": of the rows with this typology, how many got this recommendation."""
    group = [r for r in rows if r["typology"] == typology]
    return f"{sum(r['recommendation'] == recommendation for r in group)}/{len(group)}"


def core(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Recall, false escalations and failures for a subset of rows (shallow or deep)."""
    sus = [r for r in rows if r["verdict"] == "suspicious"]
    ben = [r for r in rows if r["verdict"] == "benign"]
    esc = sum(r["recommendation"] == "escalate" for r in sus)
    false_esc = sum(r["recommendation"] == "escalate" for r in ben)
    return {
        "suspicious": len(sus),
        "benign": len(ben),
        "escalated": esc,
        "recall": rate(esc, len(sus)),
        "recall_model_decided": rate(
            sum(r["recommendation"] == "escalate" and r["failure"] is None for r in sus), len(sus)
        ),
        "false_escalations": false_esc,
        "false_escalation_rate": rate(false_esc, len(ben)),
        "failures": sum(r["failure"] is not None for r in rows),
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    sus = [r for r in rows if r["verdict"] == "suspicious"]
    ben = [r for r in rows if r["verdict"] == "benign"]
    noise = [r for r in rows if r["verdict"] == "noise"]
    escalated = [r for r in sus if r["recommendation"] == "escalate"]
    decided = [r for r in escalated if r["failure"] is None]
    false_esc = [r for r in ben if r["recommendation"] == "escalate"]
    typed = [r for r in sus if r["predicted_typology"] is not None]
    answered = [
        r
        for r in rows
        if r["advice"] is not None or (r["failure"] or "").startswith(CITATION_FAILURES)
    ]
    bad_cites = [r["id"] for r in answered if (r["failure"] or "").startswith(CITATION_FAILURES)]
    injected = [r for r in rows if r["injection_txn_ids"]]
    latencies = [r["latency_s"] for r in rows]
    n = len(rows)
    return {
        "n": n,
        "suspicious": len(sus),
        "benign": len(ben),
        "recall": rate(len(escalated), len(sus)),
        "recall_ci95": wilson(len(escalated), len(sus)),
        "recall_model_decided": rate(len(decided), len(sus)),
        "missed_suspicious": [r["id"] for r in sus if r["recommendation"] != "escalate"],
        "false_escalation_rate": rate(len(false_esc), len(ben)),
        "false_escalation_ci95": wilson(len(false_esc), len(ben)),
        "false_escalations": [r["id"] for r in false_esc],
        "typology_accuracy": rate(
            sum(r["predicted_typology"] == r["typology"] for r in typed), len(typed)
        ),
        "typology_n": len(typed),
        "citation_validity": rate(len(answered) - len(bad_cites), len(answered)),
        "citation_failures": bad_cites,
        "answers": len(answered),
        "failures": dict(Counter(r["failure"].split(":")[0] for r in rows if r["failure"])),
        "failure_rate": rate(sum(r["failure"] is not None for r in rows), n),
        "injection": [
            {
                "id": r["id"],
                "verdict": r["verdict"],
                "recommendation": r["recommendation"],
                "correct": (r["recommendation"] == "escalate") == (r["verdict"] == "suspicious"),
                "memo_flagged": bool(
                    set(r["injection_txn_ids"]) & set(r["flagged_instruction_memos"])
                ),
                "failure": r["failure"],
            }
            for r in injected
        ],
        "noise_escalation_rate": rate(
            sum(r["recommendation"] == "escalate" for r in noise), len(noise)
        ),
        "noise_n": len(noise),
        "latency_p50_s": percentile(latencies, 0.5),
        "latency_p95_s": percentile(latencies, 0.95),
        "input_tokens_per_case": rate(sum(r["input_tokens"] for r in rows), n),
        "output_tokens_per_case": rate(sum(r["output_tokens"] for r in rows), n),
        "model_calls_per_case": rate(sum(r["model_calls"] for r in rows), n),
        "tool_calls_per_case": rate(sum(r["tool_calls"] for r in rows), n),
        "cost_usd_per_case": rate(sum(r["cost_usd"] for r in rows), n),
        "cost_usd_total": sum(r["cost_usd"] for r in rows),
        "by_depth": {
            "shallow": core([r for r in rows if r.get("depth", "shallow") == "shallow"]),
            "deep": core([r for r in rows if r.get("depth", "shallow") != "shallow"]),
        },
        "by_typology": {
            t: {
                "suspicious_escalated": share(sus, t, "escalate"),
                "benign_closed": share(ben, t, "close"),
            }
            for t in RULE_OF
        },
    }


MANIFESTS = {"base": HERE / "manifest.json", "deep": HERE / "deep" / "manifest.json"}


def fingerprint(reviewer: str, model_id: str, variant: str = "base") -> str:
    """Hash of everything that decides an outcome: the reviewer and model, the source of every
    module on the path (prompts, schemas, checks, bounds, tools and their docstrings, snapshot
    bounds, graph paging), the labels and the dataset hash in the manifest. A result whose
    fingerprint differs is about an older system."""
    from agentic_bank.aml import agents, case_tools, graph_source  # Strands: investigator group

    modules = [triage, agents, case_tools, graph_source, snapshot, rules]
    parts = [
        reviewer,
        model_id,
        [hashlib.sha256(inspect.getsource(m).encode()).hexdigest() for m in modules],
        hashlib.sha256(LABELS[variant].read_bytes()).hexdigest(),
        json.loads(MANIFESTS[variant].read_text())["dataset_sha256"],
    ]
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------- repeats


def spread(values: list[float]) -> dict[str, float]:
    mean = sum(values) / len(values) if values else 0.0
    return {"mean": mean, "min": min(values, default=0.0), "max": max(values, default=0.0)}


AGGREGATED: dict[str, Callable[[dict[str, Any]], float]] = {
    "recall": lambda m: m["recall"],
    "recall_model_decided": lambda m: m["recall_model_decided"],
    "false_escalation_rate": lambda m: m["false_escalation_rate"],
    "shallow_recall": lambda m: m["by_depth"]["shallow"]["recall"],
    "deep_recall": lambda m: m["by_depth"]["deep"]["recall"],
    "shallow_false_escalation_rate": lambda m: m["by_depth"]["shallow"]["false_escalation_rate"],
    "deep_false_escalation_rate": lambda m: m["by_depth"]["deep"]["false_escalation_rate"],
    "failure_rate": lambda m: m["failure_rate"],
    "typology_accuracy": lambda m: m["typology_accuracy"],
    "citation_validity": lambda m: m["citation_validity"],
    "latency_p50_s": lambda m: m["latency_p50_s"],
    "tokens_per_case": lambda m: m["input_tokens_per_case"] + m["output_tokens_per_case"],
    "cost_usd_per_case": lambda m: m["cost_usd_per_case"],
    "cost_usd_total": lambda m: m["cost_usd_total"],
}


def aggregate(metrics: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Mean, min and max of the headline metrics over repeated runs of one reviewer."""
    return {k: spread([f(m) for m in metrics]) for k, f in AGGREGATED.items()}


COMPARE_ROWS = [
    ("Recall on suspicious, all", "recall", "pct"),
    ("Recall, shallow cases", "shallow_recall", "pct"),
    ("Recall, deep cases", "deep_recall", "pct"),
    ("Recall, model-decided only", "recall_model_decided", "pct"),
    ("False escalations, all benign", "false_escalation_rate", "pct"),
    ("False escalations, shallow", "shallow_false_escalation_rate", "pct"),
    ("False escalations, deep", "deep_false_escalation_rate", "pct"),
    ("Failures (became escalate)", "failure_rate", "pct"),
    ("Typology accuracy", "typology_accuracy", "pct"),
    ("Citation validity", "citation_validity", "pct"),
    ("Latency p50", "latency_p50_s", "s"),
    ("Tokens per case", "tokens_per_case", "n"),
    ("Cost per case", "cost_usd_per_case", "usd"),
]


def fmt(a: dict[str, float], kind: str) -> str:
    if kind == "pct":
        return f"{a['mean']:.0%} ({a['min']:.0%}-{a['max']:.0%})"
    if kind == "s":
        return f"{a['mean']:.1f} s ({a['min']:.1f}-{a['max']:.1f})"
    if kind == "usd":
        return f"${a['mean']:.4f}"
    return f"{a['mean']:,.0f}"


def compare_markdown(results: dict[str, list[dict[str, Any]]]) -> str:
    """One table: a column per reviewer, mean (min-max) over its repeated runs."""
    agg = {name: aggregate([r["metrics"] for r in runs]) for name, runs in results.items()}
    header = ["Metric", *(f"{name} ({len(runs)} runs)" for name, runs in results.items())]
    return table(
        header,
        [[label, *(fmt(agg[n][key], kind) for n in results)] for label, key, kind in COMPARE_ROWS],
    )


def pct(value: float) -> str:
    return f"{value:.0%}"


def markdown(result: dict[str, Any]) -> str:
    meta, m = result["meta"], result["metrics"]
    out = [
        f"# AML triage evaluation: {meta['reviewer']}",
        "",
        f"Run {meta['date']}, reviewer `{meta['reviewer']}`, variant "
        f"`{meta.get('variant', 'base')}`, run {meta.get('run', 1)}, model `{meta['model_id']}`, "
        f"{m['n']} alerts ({m['suspicious']} suspicious, {m['benign']} benign, {m['noise_n']} "
        f"noise), fingerprint `{meta['fingerprint']}`. Generated by `python -m evals.aml`; do not "
        "edit by hand.",
        "",
        table(
            ["Metric", "Value"],
            [
                [
                    "Recall on suspicious (escalated / suspicious)",
                    f"{pct(m['recall'])} ({m['recall_ci95'][0]:.0%} to {m['recall_ci95'][1]:.0%})",
                ],
                ["Recall, model-decided escalations only", pct(m["recall_model_decided"])],
                [
                    "False-escalation rate on benign",
                    f"{pct(m['false_escalation_rate'])} ({m['false_escalation_ci95'][0]:.0%} to "
                    f"{m['false_escalation_ci95'][1]:.0%})",
                ],
                [
                    f"Typology accuracy (suspicious, n={m['typology_n']})",
                    pct(m["typology_accuracy"]),
                ],
                [f"Citation validity (answers, n={m['answers']})", pct(m["citation_validity"])],
                ["Failures (to escalate)", f"{pct(m['failure_rate'])} {m['failures'] or ''}"],
                ["Latency p50 / p95", f"{m['latency_p50_s']:.1f} s / {m['latency_p95_s']:.1f} s"],
                [
                    "Tokens per case (in / out)",
                    f"{m['input_tokens_per_case']:.0f} / {m['output_tokens_per_case']:.0f}",
                ],
                [
                    "Model calls / tool calls per case",
                    f"{m['model_calls_per_case']:.1f} / {m['tool_calls_per_case']:.1f}",
                ],
                [
                    "Cost per case / run",
                    f"${m['cost_usd_per_case']:.4f} / ${m['cost_usd_total']:.3f}",
                ],
            ],
        ),
        "",
        f"Missed suspicious: {', '.join(m['missed_suspicious']) or 'none'}. "
        f"False escalations: {', '.join(m['false_escalations']) or 'none'}.",
        "",
        "## By typology",
        "",
        table(
            ["Typology", "Suspicious escalated", "Benign closed"],
            [
                [t, v["suspicious_escalated"], v["benign_closed"]]
                for t, v in m["by_typology"].items()
            ],
        ),
        "",
        "## By depth",
        "",
        table(
            ["Depth", "Suspicious escalated", "Benign falsely escalated", "Failures"],
            [
                [
                    d,
                    f"{c['escalated']}/{c['suspicious']}",
                    f"{c['false_escalations']}/{c['benign']}",
                    c["failures"],
                ]
                for d, c in m["by_depth"].items()
            ],
        ),
        "",
        "## Prompt-injection cases",
        "",
        table(
            ["Case", "Label", "Recommendation", "Correct", "Memo flagged", "Failure"],
            [
                [
                    i["id"],
                    i["verdict"],
                    i["recommendation"],
                    "yes" if i["correct"] else "no",
                    "yes" if i["memo_flagged"] else "no",
                    i["failure"] or "-",
                ]
                for i in m["injection"]
            ],
        ),
        "",
        "## Per case",
        "",
        table(
            [
                "Case",
                "Alert",
                "Label",
                "Depth",
                "Typology",
                "Recommendation",
                "Predicted typology",
                "Failure",
            ],
            [
                [
                    r["id"],
                    f"{r['alert_id']} {r['rule_id']}",
                    r["verdict"],
                    r.get("depth", "shallow"),
                    r["typology"],
                    r["recommendation"],
                    r["predicted_typology"] or "-",
                    r["failure"] or "-",
                ]
                for r in result["rows"]
            ],
        ),
    ]
    return "\n".join(out) + "\n"


def write_result(result: dict[str, Any], out: Path) -> None:
    """`<out>.json` (summary), `<out>.md` (tables) and `<out>.cases.jsonl` (one row per case)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    summary = shorten({k: v for k, v in result.items() if k != "rows"})
    out.with_suffix(".json").write_text(json.dumps(summary, indent=2) + "\n")
    out.with_suffix(".md").write_text(markdown(result))
    lines = [json.dumps(r, ensure_ascii=False) for r in result["rows"]]
    out.with_suffix(".cases.jsonl").write_text("\n".join(lines) + "\n")
