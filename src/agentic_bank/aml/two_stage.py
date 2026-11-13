"""Two-stage reviewer: the cheap single call decides first; the graph agents only look at closes.

    stage 1  baseline (one call over the evidence pack)
             escalate, or any failure  -> escalate. Stage 2 never runs.
             close                     -> stage 2
    stage 2  graph agents (explore the whole dataset from the alert alone)
             escalate AND cite at least one transaction they READ (GraphSource read_ids) that is
             NOT in the baseline's evidence pack  -> escalate (new evidence)
             anything else                        -> the baseline's close stands, with the agents'
                                                     advice recorded for the analyst
             agent failure of any kind            -> the close stands, flagged "stage-2 failed"

Why a failed second look leaves the close in place: v1 is human-only. Every close is still seen
by an analyst, and the baseline's close already passed its own code checks. A failed stage 2 is
missing information, not evidence, so it must not invent an escalation. The analyst must see the
flag, so it is recorded in `advice["two_stage"]`.

Why "new evidence" and not just "the agents disagree": the agents on their own escalate about 70
percent of benign cases. Letting any disagreement overturn a close would bring that back.
Requiring a cited, actually read transaction outside the pack means the overturn rests on
something the single call could not have seen. Code checks it; the model does not decide it.

The result has the same signature as the other reviewers. `usage` is the sum of both stages. The
per-stage numbers are in `audit["two_stage"]`.
"""

import time
from collections.abc import Callable
from typing import Any

from agentic_bank.aml.case_tools import txn_ids
from agentic_bank.aml.triage import TriageOutcome, TriageRun

Reviewer = Callable[[Any, str, dict[str, Any]], TriageRun]


def cited_risk_ids(advice: dict[str, Any] | None) -> set[str]:
    """Transaction ids cited in the risk factors of an agent answer."""
    return {i for f in (advice or {}).get("risk_factors", []) for i in f["txn_ids"]}


def stage_info(run: TriageRun) -> dict[str, Any]:
    return {
        "recommendation": run.outcome.recommendation,
        "failure": run.outcome.failure,
        "input_tokens": run.usage.get("inputTokens", 0),
        "output_tokens": run.usage.get("outputTokens", 0),
        "model_calls": run.usage.get("model_calls", 0),
        "seconds": run.seconds,
    }


def two_stage(baseline: Reviewer, graph: Reviewer) -> Reviewer:
    def run_two_stage(bedrock: Any, model_id: str, snapshot: dict[str, Any]) -> TriageRun:
        t0 = time.monotonic()
        first = baseline(bedrock, model_id, snapshot)
        first.seconds = first.seconds or round(time.monotonic() - t0, 2)
        info: dict[str, Any] = {"stage1": stage_info(first), "stage2": None}

        def done(run: TriageRun, decided_by: str, **extra: Any) -> TriageRun:
            info.update(decided_by=decided_by, **extra)
            run.audit = {**run.audit, "two_stage": info}
            return run

        if first.outcome.recommendation == "escalate" or first.outcome.failure:
            first.audit = {}
            return done(first, "stage1_escalate" if not first.outcome.failure else "stage1_failure")

        # Stage 1 closed. Stage 2 explores from the alert alone.
        t1 = time.monotonic()
        second = graph(bedrock, model_id, {"alert": snapshot["alert"]})
        second.seconds = second.seconds or round(time.monotonic() - t1, 2)
        info["stage2"] = stage_info(second)
        usage = {
            k: first.usage.get(k, 0) + second.usage.get(k, 0)
            for k in ("inputTokens", "outputTokens", "model_calls")
        }
        advice = dict(first.outcome.advice or {})
        note: dict[str, Any] = {"stage2_advice": second.outcome.advice}
        advice["two_stage"] = note
        merged = TriageRun(
            outcome=TriageOutcome("close", None, advice),
            usage=usage,
            seconds=round(first.seconds + second.seconds, 2),
            tool_calls=second.tool_calls,
            refused_tools=second.refused_tools,
            audit=second.audit,
        )

        if second.outcome.failure:
            note["stage2_failed"] = second.outcome.failure
            return done(merged, "stage2_failed_close_stands")
        if second.outcome.recommendation == "escalate":
            read = set(second.audit.get("read_ids", []))
            new = sorted((cited_risk_ids(second.outcome.advice) & read) - txn_ids(snapshot))
            if new:
                note["new_evidence_ids"] = new
                merged.outcome = TriageOutcome("escalate", None, advice)
                return done(merged, "stage2_escalate_new_evidence", new_evidence_ids=new)
            return done(merged, "stage2_escalate_no_new_evidence_close_stands")
        return done(merged, "stage2_close")

    return run_two_stage
