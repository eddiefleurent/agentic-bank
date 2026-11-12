"""Evaluation harness for the dispute reviewer: labeled cases in, error rates out.

It runs the production path around the model: `build_case_file` and `code_scores` make the facts,
the reviewer under test scores them, and `decide` turns that into AUTO_REFUND, ASK or ANALYST. The
outcome is compared with the label. Any callable with the signature of `review.run_review` can be
the reviewer, so a different implementation is measured on the same cases with the same rules.

No AWS call is made here except through the `bedrock` client handed to `evaluate`.
"""

import hashlib
import json
import math
import random
import time
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentic_bank.disputes import casefile, decide, review, tracing
from agentic_bank.disputes.review import ReviewOutcome
from agentic_bank.network.order_insight import Order

# reviewer(bedrock, model_id, guardrail_id, guardrail_version, case_file) -> ReviewOutcome
Reviewer = Callable[[Any, str, str, str, dict[str, Any]], ReviewOutcome]

HERE = Path(__file__).parent
CASES = HERE / "cases.jsonl"
BOUNDS = HERE / "bounds.json"
OUTCOMES = ("AUTO_REFUND", "ASK", "ANALYST")
CATEGORIES = ("clear_refund", "clear_deny", "ambiguous", "injection", "pii")
REASONS = ("not_received", "not_recognised", "other")
GROUNDING_FAILURE = "grounding below threshold"  # the failure text review.run_review sets

# On-demand list prices, us-east-1, read from the AWS Price List API on 2026-11-12 (model) and the
# Bedrock pricing page (contextual grounding). Change them if you change model, tier or region.
USD_PER_1K_INPUT = 0.00015
USD_PER_1K_OUTPUT = 0.00065
USD_PER_GROUNDING_UNIT = 0.0001  # 1 unit = up to 1,000 characters of source + query + content
# An assumption, not a measurement: what one analyst review costs the bank. The threshold pick
# weighs it against the money lost to false refunds. Replace it with your own figure.
ANALYST_REVIEW_USD = 10.0


@dataclass(frozen=True)
class Case:
    id: str
    category: str
    reason_code: str
    amount_cents: int
    statement: str
    expected: str  # the label: AUTO_REFUND | ASK | ANALYST
    rationale: str  # why the label is what it is
    order: dict[str, Any] | None = None
    answer: str | None = None  # the customer's reply to the round-0 question, if any
    round: int = 0
    expected_c4: int | None = None  # human label for the one criterion only the model scores
    canaries: list[str] = field(default_factory=list)  # strings the review must never contain


def load_cases(path: Path = CASES) -> list[Case]:
    cases = [Case(**json.loads(line)) for line in path.read_text().splitlines() if line.strip()]
    for c in cases:
        ok = c.category in CATEGORIES and c.expected in OUTCOMES and c.reason_code in REASONS
        if not ok or c.expected_c4 not in (None, -1, 0, 1) or not c.rationale:
            raise ValueError(f"bad case {c.id}")
    if len({c.id for c in cases}) != len(cases):
        raise ValueError("duplicate case ids")
    return cases


def stratified_sample(cases: list[Case], n: int, seed: int = 7) -> list[Case]:
    """About `n` cases with every category represented in proportion. Same seed, same sample."""
    rng = random.Random(seed)
    picked: list[Case] = []
    for category in CATEGORIES:
        group = [c for c in cases if c.category == category]
        if group:
            picked += rng.sample(group, min(len(group), max(1, round(n * len(group) / len(cases)))))
    return picked


def built(case: Case) -> dict[str, Any]:
    """What the workflow's Build step produces for this case, with the production functions."""
    order = Order(**case.order) if case.order else None
    row = {
        "reason_code": case.reason_code,
        "amount_cents": case.amount_cents,
        "statement_tok": case.statement,
        "answer_tok": case.answer,
    }
    return {
        "case_file": casefile.build_case_file(row, order),
        "code_scores": casefile.code_scores(case.reason_code, order),
        "has_order": order is not None,
    }


def outcome_for(case: Case, outcome: ReviewOutcome) -> decide.Decision:
    facts = built(case)
    return decide.decide(
        reason_code=case.reason_code,
        amount_cents=case.amount_cents,
        has_order=facts["has_order"],
        code_scores=facts["code_scores"],
        review=outcome,
        round_no=case.round,
    )


class Meter:
    """Wraps the Bedrock runtime client handed to one reviewer call and counts what it uses.

    A reviewer that builds its own client is not metered: its row shows latency only.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self.input_tokens = self.output_tokens = self.model_calls = self.grounding_units = 0

    def converse(self, **kwargs: Any) -> Any:
        response = self._client.converse(**kwargs)
        usage = response.get("usage", {})
        self.model_calls += 1
        self.input_tokens += int(usage.get("inputTokens", 0))
        self.output_tokens += int(usage.get("outputTokens", 0))
        return response

    def apply_guardrail(self, **kwargs: Any) -> Any:
        response = self._client.apply_guardrail(**kwargs)
        self.grounding_units += int(
            response.get("usage", {}).get("contextualGroundingPolicyUnits", 0)
        )
        return response

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    @property
    def cost_usd(self) -> float:
        return (
            self.input_tokens / 1000 * USD_PER_1K_INPUT
            + self.output_tokens / 1000 * USD_PER_1K_OUTPUT
            + self.grounding_units * USD_PER_GROUNDING_UNIT
        )


def evaluate_case(
    case: Case, reviewer: Reviewer, bedrock: Any, model_id: str, guardrail_id: str, version: str
) -> dict[str, Any]:
    facts = built(case)
    meter = Meter(bedrock)
    with tracing.span("dispute.evaluate", {"eval.case_id": case.id}) as span:
        start = time.perf_counter()
        try:
            outcome = reviewer(meter, model_id, guardrail_id, version, facts["case_file"])
        except Exception as error:  # in production a failed Review step routes to an analyst too
            outcome = ReviewOutcome(failure=f"reviewer raised {type(error).__name__}")
        latency = time.perf_counter() - start
        decision = outcome_for(case, outcome)
        tracing.put(
            span,
            {
                "eval.expected": case.expected,
                "dispute.decision.outcome": decision.outcome,
                "dispute.decision.rule": decision.rule,
            },
        )
    text = json.dumps(outcome.review, ensure_ascii=False).lower() if outcome.review else ""
    scores = {c["id"]: c["score"] for c in (outcome.review or {}).get("criteria", [])}
    return {
        "id": case.id,
        "category": case.category,
        "expected": case.expected,
        "predicted": decision.outcome,
        "rule": decision.rule,
        "expected_c4": case.expected_c4,
        "model_scores": scores,
        "flags": (outcome.review or {}).get("flags", []),
        "code_scores": facts["code_scores"],
        "grounding": outcome.grounding,
        "failure": outcome.failure,
        "leaked": [c for c in case.canaries if c.lower() in text],
        "latency_s": round(latency, 3),
        "input_tokens": meter.input_tokens,
        "output_tokens": meter.output_tokens,
        "model_calls": meter.model_calls,
        "grounding_units": meter.grounding_units,
        "cost_usd": round(meter.cost_usd, 6),
        "review": outcome.review,
    }


def evaluate(
    cases: Iterable[Case],
    reviewer: Reviewer,
    bedrock: Any,
    model_id: str,
    guardrail_id: str,
    version: str,
    workers: int = 6,
) -> list[dict[str, Any]]:
    def one(case: Case) -> dict[str, Any]:
        return evaluate_case(case, reviewer, bedrock, model_id, guardrail_id, version)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, cases))


# ---------------------------------------------------------------------------- metrics


def wilson(successes: int, n: int, z: float = 1.96) -> list[float]:
    """95% Wilson score interval for a proportion: honest about small samples, never below 0."""
    if n == 0:
        return [0.0, 1.0]
    p = successes / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return [
        max(0.0, (centre - spread) / (1 + z * z / n)),
        min(1.0, (centre + spread) / (1 + z * z / n)),
    ]


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile; 0.0 for an empty list."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def rate(part: int, whole: int) -> float:
    return part / whole if whole else 0.0


def core(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Outcome metrics for a set of rows. False-refund rate is over cases that should NOT be
    auto-refunded: the share of them the pipeline paid without a person."""
    n = len(rows)
    agree = sum(r["predicted"] == r["expected"] for r in rows)
    should_not = [r for r in rows if r["expected"] != "AUTO_REFUND"]
    should = [r for r in rows if r["expected"] == "AUTO_REFUND"]
    false_refunds = [r["id"] for r in should_not if r["predicted"] == "AUTO_REFUND"]
    refunds = sum(r["predicted"] == "AUTO_REFUND" for r in rows)
    return {
        "n": n,
        "agreement": rate(agree, n),
        "agreement_ci95": wilson(agree, n),
        "false_refunds": false_refunds,
        "false_refund_rate": rate(len(false_refunds), len(should_not)),
        "false_refund_ci95": wilson(len(false_refunds), len(should_not)),
        "not_refundable_cases": len(should_not),
        "wrong_share_of_auto_refunds": rate(len(false_refunds), refunds),
        "missed_refunds": sum(r["predicted"] != "AUTO_REFUND" for r in should),
        "missed_refund_rate": rate(
            sum(r["predicted"] != "AUTO_REFUND" for r in should), len(should)
        ),
        "auto_refund_rate": rate(refunds, n),
        "ask_rate": rate(sum(r["predicted"] == "ASK" for r in rows), n),
        "escalation_rate": rate(sum(r["predicted"] == "ANALYST" for r in rows), n),
        "expected_escalation_rate": rate(sum(r["expected"] == "ANALYST" for r in rows), n),
        "canary_leaks": [r["id"] for r in rows if r["leaked"]],
    }


def judge(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """How well the model judges. C1-C3 have a deterministic answer (code's score); C4 is compared
    with the human label where one was given."""
    facts = [
        (cid, r["model_scores"][cid] == score)
        for r in rows
        for cid, score in r["code_scores"].items()
        if cid in r["model_scores"]
    ]
    c4 = [r for r in rows if r["expected_c4"] is not None and "C4" in r["model_scores"]]
    c4_agree = sum(r["model_scores"]["C4"] == r["expected_c4"] for r in c4)
    confusion = {str(e): {str(p): 0 for p in (-1, 0, 1)} for e in (-1, 0, 1)}
    for r in c4:
        confusion[str(r["expected_c4"])][str(r["model_scores"]["C4"])] += 1
    per = {}
    for cid in ("C1", "C2", "C3"):
        hits = [ok for i, ok in facts if i == cid]
        per[cid] = {"n": len(hits), "agreement": rate(sum(hits), len(hits))}
    return {
        "facts_n": len(facts),
        "facts_agreement": rate(sum(ok for _, ok in facts), len(facts)),
        "facts_by_criterion": per,
        "c4_n": len(c4),
        "c4_agreement": rate(c4_agree, len(c4)),
        "c4_agreement_ci95": wilson(c4_agree, len(c4)),
        "c4_confusion": confusion,  # [label][model]
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    confusion = {e: {p: 0 for p in OUTCOMES} for e in OUTCOMES}
    for r in rows:
        confusion[r["expected"]][r["predicted"]] += 1
    latencies = [r["latency_s"] for r in rows]
    failures = Counter(r["failure"].split(":")[0] for r in rows if r["failure"])
    n = len(rows)
    return {
        **core(rows),
        "confusion": confusion,  # [label][pipeline]
        "rules": dict(sorted(Counter(str(r["rule"]) for r in rows).items())),
        "review_failures": dict(failures),
        "latency_p50_s": percentile(latencies, 0.5),
        "latency_p95_s": percentile(latencies, 0.95),
        "input_tokens_per_case": rate(sum(r["input_tokens"] for r in rows), n),
        "output_tokens_per_case": rate(sum(r["output_tokens"] for r in rows), n),
        "model_calls_per_case": rate(sum(r["model_calls"] for r in rows), n),
        "cost_usd_per_case": rate(sum(r["cost_usd"] for r in rows), n),
        "cost_usd_total": sum(r["cost_usd"] for r in rows),
        "judge": judge(rows),
        "by_category": {
            c: core([r for r in rows if r["category"] == c])
            for c in CATEGORIES
            if any(r["category"] == c for r in rows)
        },
    }


# ---------------------------------------------------------------------------- threshold sweep


def rescore(row: dict[str, Any], case: Case, grounding_min: float, relevance_min: float) -> str:
    """The outcome this row would have had under other grounding thresholds. No model call: the
    guardrail's raw scores are in the row, and only the pass/fail cut moves."""
    failure, scores = row["failure"], row["grounding"] or {}
    g, r = scores.get("grounding"), scores.get("relevance")
    if failure in (None, GROUNDING_FAILURE) and g is not None and r is not None:
        failure = None if g >= grounding_min and r >= relevance_min else GROUNDING_FAILURE
    outcome = ReviewOutcome(review=row["review"], grounding=row["grounding"], failure=failure)
    return outcome_for(case, outcome).outcome


def sweep_point(
    rows: list[dict[str, Any]], cases: dict[str, Case], grounding_min: float, relevance_min: float
) -> dict[str, Any]:
    moved = [
        {**r, "predicted": rescore(r, cases[r["id"]], grounding_min, relevance_min)} for r in rows
    ]
    m = core(moved)
    lost = sum(cases[i].amount_cents for i in m["false_refunds"]) / 100
    analyst = sum(r["predicted"] == "ANALYST" for r in moved)
    return {
        "grounding_min": grounding_min,
        "relevance_min": relevance_min,
        "auto_refund_rate": m["auto_refund_rate"],
        "false_refunds": len(m["false_refunds"]),
        "false_refund_rate": m["false_refund_rate"],
        "false_refund_usd": lost,
        "missed_refund_rate": m["missed_refund_rate"],
        "agreement": m["agreement"],
        "escalation_rate": m["escalation_rate"],
        "cost_usd": lost + analyst * ANALYST_REVIEW_USD,  # money wrongly paid + analyst time
    }


def sweep(rows: list[dict[str, Any]], cases: list[Case]) -> dict[str, Any] | None:
    """Trade-off curves: one threshold moves, the other stays at the deployed value. None when the
    reviewer reports no grounding scores (nothing to sweep)."""
    if not any((r["grounding"] or {}).get("grounding") is not None for r in rows):
        return None
    by_id = {c.id: c for c in cases}
    steps = [round(i * 0.05, 2) for i in range(21)]
    g0, r0 = review.GROUNDING_MIN, review.RELEVANCE_MIN
    return {
        "deployed": {"grounding_min": g0, "relevance_min": r0},
        "grounding": [sweep_point(rows, by_id, g, r0) for g in steps],
        "relevance": [sweep_point(rows, by_id, g0, r) for r in steps],
        "no_check": sweep_point(rows, by_id, 0.0, 0.0),
    }


def recommend(points: list[dict[str, Any]], key: str) -> dict[str, Any]:
    """Pick a threshold from a curve by cost: dollars wrongly refunded plus analyst reviews at
    ANALYST_REVIEW_USD each. Among equally cheap thresholds take the strictest, so the check still
    trips on the worst output. `plateau` is the whole equally cheap range."""
    cheapest = min(p["cost_usd"] for p in points)
    plateau = [p for p in points if p["cost_usd"] == cheapest]
    pick = plateau[-1]
    return {
        "threshold": pick[key],
        "plateau": [plateau[0][key], pick[key]],
        "cost_usd": cheapest,
        "false_refunds": pick["false_refunds"],
        "agreement": pick["agreement"],
        "auto_refund_rate": pick["auto_refund_rate"],
    }


# ---------------------------------------------------------------------------- gate and report


def check_gate(metrics: dict[str, Any], bounds: dict[str, Any], sampled: bool = False) -> list[str]:
    """Violations of the configured bounds; an empty list means the gate passes.

    A full run is held to the bound itself. A sample is small and noisy, so it fails only when its
    95% interval excludes the bound: a smoke test for breakage, not an acceptance test."""
    false_rate, agreement = metrics["false_refund_rate"], metrics["agreement"]
    if sampled:
        false_rate, agreement = metrics["false_refund_ci95"][0], metrics["agreement_ci95"][1]
    problems = []
    if false_rate > bounds["max_false_refund_rate"]:
        problems.append(
            f"false-refund rate {metrics['false_refund_rate']:.1%} > "
            f"{bounds['max_false_refund_rate']:.1%} ({', '.join(metrics['false_refunds'])})"
        )
    if agreement < bounds["min_agreement"]:
        problems.append(f"agreement {metrics['agreement']:.1%} < {bounds['min_agreement']:.1%}")
    if len(metrics["canary_leaks"]) > bounds["max_canary_leaks"]:
        problems.append(f"canary leaks: {', '.join(metrics['canary_leaks'])}")
    return problems


def fingerprint(model_id: str, cases_path: Path = CASES) -> str:
    """Hash of everything that decides an outcome: prompt, thresholds, cap, model and dataset. A
    committed result whose fingerprint differs from the code is evidence about an older system."""
    parts = [
        review.RUBRIC,
        review.QUERY,
        review.review_schema(["C1", "C2", "C3", "C4"]),
        review.TOOL_CHOICES,
        review.GROUNDING_MIN,
        review.RELEVANCE_MIN,
        review.MAX_OUTPUT_TOKENS,
        decide.AUTO_REFUND_MAX_CENTS,
        model_id,
        hashlib.sha256(cases_path.read_bytes()).hexdigest(),
    ]
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:16]


def pct(value: float) -> str:
    return f"{value:.1%}"


def ci(pair: list[float]) -> str:
    return f"{pair[0]:.1%} to {pair[1]:.1%}"


def table(header: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + " --- |" * len(header)]
    return "\n".join(lines + ["| " + " | ".join(str(v) for v in row) + " |" for row in rows])


def curve(points: list[dict[str, Any]], key: str, deployed: float) -> str:
    header = ["Threshold", "Auto-refund rate", "False refunds", "False-refund rate", "Agreement"]
    return table(
        [*header, "To analyst", "Cost"],
        [
            [
                f"{p[key]:.2f}" + (" (deployed)" if p[key] == deployed else ""),
                pct(p["auto_refund_rate"]),
                p["false_refunds"],
                pct(p["false_refund_rate"]),
                pct(p["agreement"]),
                pct(p["escalation_rate"]),
                f"${p['cost_usd']:.0f}",
            ]
            for p in points
        ],
    )


def markdown(result: dict[str, Any]) -> str:
    meta, m, j = result["meta"], result["metrics"], result["metrics"]["judge"]
    out = [
        f"# Dispute reviewer evaluation: {meta['label']}",
        "",
        f"Run {meta['date']}, reviewer `{meta['reviewer']}`, model `{meta['model_id']}`, "
        f"{m['n']} of {meta['dataset_size']} cases, fingerprint `{meta['fingerprint']}`. "
        "Generated by `python -m evals.disputes`; do not edit by hand.",
        "",
        "## Headline",
        "",
        table(
            ["Metric", "Value", "95% interval"],
            [
                ["Agreement with labels", pct(m["agreement"]), ci(m["agreement_ci95"])],
                [
                    f"False-refund rate ({len(m['false_refunds'])} of {m['not_refundable_cases']} "
                    "cases that should not be auto-refunded)",
                    pct(m["false_refund_rate"]),
                    ci(m["false_refund_ci95"]),
                ],
                [
                    "Missed refunds (label says refund, pipeline did not)",
                    pct(m["missed_refund_rate"]),
                    "",
                ],
                ["Auto-refund rate (decided with no person)", pct(m["auto_refund_rate"]), ""],
                ["Question to customer", pct(m["ask_rate"]), ""],
                [
                    f"Escalation to analyst (labels expect {pct(m['expected_escalation_rate'])})",
                    pct(m["escalation_rate"]),
                    "",
                ],
                ["Canary leaks (private or planted text repeated)", len(m["canary_leaks"]), ""],
                [
                    "Latency p50 / p95",
                    f"{m['latency_p50_s']:.1f} s / {m['latency_p95_s']:.1f} s",
                    "",
                ],
                [
                    "Tokens per case (in / out)",
                    f"{m['input_tokens_per_case']:.0f} / {m['output_tokens_per_case']:.0f}",
                    "",
                ],
                [
                    "Cost per case / this run",
                    f"${m['cost_usd_per_case']:.5f} / ${m['cost_usd_total']:.3f}",
                    "",
                ],
            ],
        ),
        "",
        f"False refunds: {', '.join(m['false_refunds']) or 'none'}. "
        f"Canary leaks: {', '.join(m['canary_leaks']) or 'none'}. "
        f"Review failures: {m['review_failures'] or 'none'}. Decision rules hit: {m['rules']}.",
        "",
        "## Confusion matrix (rows: label, columns: pipeline)",
        "",
        table(
            ["Label", *OUTCOMES],
            [[e, *[m["confusion"][e][p] for p in OUTCOMES]] for e in OUTCOMES],
        ),
        "",
        "## By category",
        "",
        table(
            [
                "Category",
                "Cases",
                "Agreement",
                "False refunds",
                "Missed refunds",
                "To analyst",
                "Leaks",
            ],
            [
                [
                    name,
                    c["n"],
                    pct(c["agreement"]),
                    len(c["false_refunds"]),
                    c["missed_refunds"],
                    pct(c["escalation_rate"]),
                    len(c["canary_leaks"]),
                ]
                for name, c in m["by_category"].items()
            ],
        ),
        "",
        "## The model as a judge",
        "",
        f"C1-C3 against code's score: {pct(j['facts_agreement'])} of {j['facts_n']} scores ("
        + ", ".join(
            f"{k} {pct(v['agreement'])} of {v['n']}" for k, v in j["facts_by_criterion"].items()
        )
        + f"). C4 against the human label: {pct(j['c4_agreement'])} of {j['c4_n']} "
        f"(95% interval {ci(j['c4_agreement_ci95'])}).",
        "",
        table(
            ["C4 label", "model -1", "model 0", "model +1"],
            [[e, *[j["c4_confusion"][e][p] for p in ("-1", "0", "1")]] for e in ("-1", "0", "1")],
        ),
    ]
    s = result["sweep"]
    if s:
        rec, none = result["recommendation"], s["no_check"]
        out += [
            "",
            "## Grounding threshold sweep",
            "",
            "Recomputed from the stored guardrail scores; no extra model calls. With no grounding "
            f"check at all: auto-refund rate {pct(none['auto_refund_rate'])}, "
            f"{none['false_refunds']} false refunds, agreement {pct(none['agreement'])}. Cost is "
            f"the money wrongly refunded plus ${ANALYST_REVIEW_USD:.0f} (assumed) per analyst "
            "review; the pick is the strictest threshold among the cheapest.",
            "",
            f"Grounding threshold (relevance fixed at {s['deployed']['relevance_min']}). "
            f"Pick: **{rec['grounding']['threshold']:.2f}** (equally cheap from "
            f"{rec['grounding']['plateau'][0]:.2f} to {rec['grounding']['plateau'][1]:.2f}).",
            "",
            curve(s["grounding"], "grounding_min", s["deployed"]["grounding_min"]),
            "",
            f"Relevance threshold (grounding fixed at {s['deployed']['grounding_min']}). "
            f"Pick: **{rec['relevance']['threshold']:.2f}** (equally cheap from "
            f"{rec['relevance']['plateau'][0]:.2f} to {rec['relevance']['plateau'][1]:.2f}).",
            "",
            curve(s["relevance"], "relevance_min", s["deployed"]["relevance_min"]),
        ]
    wrong = [r for r in result["rows"] if r["predicted"] != r["expected"]]
    if wrong:
        out += [
            "",
            "## Disagreements",
            "",
            table(
                ["Case", "Label", "Pipeline", "Rule", "Model scores", "Code scores", "Failure"],
                [
                    [
                        r["id"],
                        r["expected"],
                        r["predicted"],
                        r["rule"],
                        " ".join(f"{k}={v}" for k, v in sorted(r["model_scores"].items())) or "-",
                        " ".join(f"{k}={v}" for k, v in sorted(r["code_scores"].items())) or "-",
                        (r["failure"] or "-").split(":")[0],
                    ]
                    for r in wrong
                ],
            ),
        ]
    return "\n".join(out) + "\n"


def build_result(
    rows: list[dict[str, Any]], cases: list[Case], meta: dict[str, Any]
) -> dict[str, Any]:
    curves = sweep(rows, cases)
    recommendation = curves and {
        "grounding": recommend(curves["grounding"], "grounding_min"),
        "relevance": recommend(curves["relevance"], "relevance_min"),
    }
    return {
        "meta": meta,
        "metrics": summarize(rows),
        "sweep": curves,
        "recommendation": recommendation,
        "rows": rows,
    }


def shorten(value: Any) -> Any:
    """Floats to 4 decimals, so the JSON diffs cleanly."""
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, dict):
        return {k: shorten(v) for k, v in value.items()}
    if isinstance(value, list):
        return [shorten(v) for v in value]
    return value


def write_result(result: dict[str, Any], out: Path) -> None:
    """`<out>.json` (summary), `<out>.md` (tables) and `<out>.cases.jsonl` (one row per case)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    summary = shorten({k: v for k, v in result.items() if k != "rows"})
    out.with_suffix(".json").write_text(json.dumps(summary, indent=2) + "\n")
    out.with_suffix(".md").write_text(markdown(result))
    lines = [json.dumps(r, ensure_ascii=False) for r in result["rows"]]
    out.with_suffix(".cases.jsonl").write_text("\n".join(lines) + "\n")
