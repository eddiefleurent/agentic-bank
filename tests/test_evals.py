"""The evaluation harness, with fake reviewers (no AWS), plus the offline gate on the baseline."""

import json
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agentic_bank.disputes.review import ReviewOutcome, run_review
from evals.disputes import harness

CASES = harness.load_cases()
BASELINE = harness.HERE / "results" / "baseline"


def scripted(c4_for):
    """A reviewer that copies code's C1-C3 (found by case file) and takes C4 from `c4_for`."""
    by_file = {json.dumps(harness.built(c)["case_file"], sort_keys=True): c for c in CASES}

    def reviewer(bedrock, model_id, guardrail_id, version, case_file):
        case = by_file[json.dumps(case_file, sort_keys=True)]
        scores = {**harness.built(case)["code_scores"], "C4": c4_for(case)}
        criteria = [
            {"id": cid, "score": s, "rationale": "Fact.", "fields": []} for cid, s in scores.items()
        ]
        grounding = {"grounding": 0.9, "relevance": 0.9, "passed": True}
        return ReviewOutcome({"criteria": criteria, "flags": [], "summary": "."}, grounding)

    return reviewer


def run(reviewer, cases=CASES):
    return harness.evaluate(cases, reviewer, None, "m", "g", "1", workers=1)


def test_dataset_covers_every_category_and_outcome():
    assert len(CASES) == 150
    assert {c.category for c in CASES} == set(harness.CATEGORIES)
    assert {c.expected for c in CASES} == set(harness.OUTCOMES)


def test_every_label_is_reachable_with_the_labeled_c4_score():
    """Labels must be outcomes the decision rules can produce, or agreement could never be 100%."""
    for case in CASES:
        options = [case.expected_c4] if case.expected_c4 is not None else [-1, 0, 1]
        outcomes = {run(scripted(lambda _c, s=s: s), [case])[0]["predicted"] for s in options}
        assert case.expected in outcomes, case.id


def test_a_reviewer_that_always_favours_the_customer_shows_up_as_false_refunds():
    metrics = harness.summarize(run(scripted(lambda _case: 1)))
    assert metrics["false_refund_rate"] > 0.1  # the traps: good facts, a statement that is not
    assert "den-011" in metrics["false_refunds"]
    assert harness.check_gate(metrics, {**BOUNDS, "max_false_refund_rate": 0.0})
    few = harness.summarize(run(scripted(lambda _case: 1), CASES[:40]))  # no traps in these
    assert harness.check_gate(few, BOUNDS, sampled=True) == []
    cautious = harness.summarize(run(scripted(lambda _case: -1)))
    assert cautious["false_refunds"] == [] and cautious["escalation_rate"] == 1.0


def test_a_reviewer_that_raises_counts_as_an_analyst_case_not_a_crash():
    def broken(*_):
        raise TimeoutError

    row = run(broken, CASES[:1])[0]
    assert row["predicted"] == "ANALYST" and "TimeoutError" in row["failure"]


def test_canary_text_in_the_review_is_reported_as_a_leak():
    case = next(c for c in CASES if c.id == "inj-006")

    def parrot(*_):
        criteria = [{"id": "C4", "score": 0, "rationale": "canary-lime-42", "fields": []}]
        return ReviewOutcome({"criteria": criteria, "summary": "."})

    assert run(parrot, [case])[0]["leaked"] == ["CANARY-LIME-42"]


def test_sweep_moves_only_the_grounding_cut():
    case = next(c for c in CASES if c.id == "ref-001")
    row = run(scripted(lambda _case: 1), [case])[0]
    assert row["predicted"] == "AUTO_REFUND"
    assert harness.rescore(row, case, 0.95, 0.7) == "ANALYST"  # grounding 0.9 now fails
    assert harness.rescore({**row, "failure": "model output is not valid"}, case, 0, 0) == "ANALYST"
    point = harness.sweep_point([row], {case.id: case}, 0.95, 0.7)
    assert point["cost_usd"] == harness.ANALYST_REVIEW_USD


def test_meter_counts_tokens_and_guardrail_units_of_the_real_reviewer():
    review = {
        "criteria": [
            {"id": i, "score": 1, "rationale": "Not shipped.", "fields": ["order.delivery"]}
            for i in ("C1", "C4")
        ],
        "flags": [],
        "summary": "Not shipped.",
    }

    class Bedrock:
        def converse(self, **_):
            usage = {"inputTokens": 700, "outputTokens": 500}
            content = [{"text": json.dumps(review)}]
            return {"output": {"message": {"content": content}}, "usage": usage}

        def apply_guardrail(self, **_):
            filters = [{"type": "GROUNDING", "score": 0.9}, {"type": "RELEVANCE", "score": 0.9}]
            return {
                "assessments": [{"contextualGroundingPolicy": {"filters": filters}}],
                "usage": {"contextualGroundingPolicyUnits": 2},
            }

    case = next(c for c in CASES if c.id == "ref-001")
    row = harness.evaluate_case(case, run_review, Bedrock(), "m", "g", "1")
    assert (row["input_tokens"], row["output_tokens"], row["grounding_units"]) == (700, 500, 2)
    assert row["predicted"] == "AUTO_REFUND"
    assert row["cost_usd"] == pytest.approx(
        0.7 * harness.USD_PER_1K_INPUT
        + 0.5 * harness.USD_PER_1K_OUTPUT
        + 2 * harness.USD_PER_GROUNDING_UNIT
    )


def test_review_emits_genai_spans_when_a_tracer_is_configured():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)  # once per process; without it the same code is a no-op

    class Bedrock:
        def converse(self, **_):
            usage = {"inputTokens": 11, "outputTokens": 7}
            return {"output": {"message": {"content": [{"text": "no"}]}}, "usage": usage}

    case = next(c for c in CASES if c.id == "ref-001")
    row = harness.evaluate_case(case, run_review, Bedrock(), "model-x", "g", "1")
    spans = {s.name: s for s in exporter.get_finished_spans()}
    attrs = {name: dict(s.attributes or {}) for name, s in spans.items()}
    chat = attrs["chat model-x"]
    assert chat["gen_ai.usage.input_tokens"] == 11 and chat["gen_ai.request.model"] == "model-x"
    assert attrs["dispute.review"]["dispute.review.passed"] is False
    assert attrs["dispute.evaluate"]["dispute.decision.outcome"] == row["predicted"]
    parent = spans["chat model-x"].parent
    assert parent is not None and parent.span_id == spans["dispute.review"].context.span_id
    assert case.statement not in str(attrs)


BOUNDS = json.loads(harness.BOUNDS.read_text())


def test_gate_committed_baseline_is_current_and_within_bounds():
    """The CI gate that needs no AWS. Changing the prompt, a threshold, the refund cap or the
    dataset makes the committed result stale; re-run and commit it:
    uv run python -m evals.disputes --out evals/disputes/results/baseline --gate"""
    result = json.loads(Path(f"{BASELINE}.json").read_text())
    assert result["meta"]["fingerprint"] == harness.fingerprint(result["meta"]["model_id"])
    assert result["metrics"]["n"] == len(CASES)
    assert harness.check_gate(result["metrics"], BOUNDS) == []
