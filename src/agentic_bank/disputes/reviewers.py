"""Which reviewer judges a case file, chosen by config (env REVIEWER). All return ReviewOutcome.

  classifier    one model call, no tools (review.run_review). The default, and what is deployed.
  investigator  the Strands investigator in this process (local runs and the eval harness; needs
                the `investigator` dependency group, which the Lambda zip does not carry).
  runtime       the same investigator hosted on AgentCore Runtime; this Lambda only invokes it.
                Planned, not deployed (infra/app/investigator.tf, investigator_enabled = false).

Whichever runs, its output passes the same checks and code decides (disputes/decide.py).
"""

import dataclasses
import json
import os
import uuid
from collections.abc import Callable
from functools import partial
from typing import Any

from agentic_bank.disputes.review import ReviewOutcome, run_review

Reviewer = Callable[[Any, str, str, str, dict[str, Any]], ReviewOutcome]
NAMES = ("classifier", "investigator", "runtime")


def select(name: str, *, customer: str | None = None, case_id: str | None = None) -> Reviewer:
    if name == "classifier":
        return run_review
    if name == "investigator":
        from agentic_bank.investigator.agent import run_investigation  # Strands: optional

        return run_investigation
    if name == "runtime":
        return partial(invoke_runtime, customer=customer, case_id=case_id)
    raise ValueError(f"unknown reviewer {name!r}; expected one of {NAMES}")


def invoke_runtime(
    _bedrock: Any,
    _model_id: str,
    _guardrail_id: str,
    _version: str,
    case_file: dict[str, Any],
    *,
    customer: str | None,
    case_id: str | None,
    client: Any = None,
) -> ReviewOutcome:
    """Ask the AgentCore Runtime investigator. Model, guardrail and bounds are its own config.
    Any error or unexpected reply is a failure marker (rule 1: analyst), never a refund."""
    try:
        import boto3

        client = client or boto3.client("bedrock-agentcore")
        response = client.invoke_agent_runtime(
            agentRuntimeArn=os.environ["INVESTIGATOR_RUNTIME_ARN"],
            qualifier="DEFAULT",
            runtimeSessionId=f"{case_id or 'case'}-{uuid.uuid4().hex}",  # >= 33 chars
            contentType="application/json",
            accept="application/json",
            payload=json.dumps({"case_file": case_file, "customer": customer, "case_id": case_id}),
        )
        body = json.loads(response["response"].read())
        names = {f.name for f in dataclasses.fields(ReviewOutcome)}
        if not isinstance(body, dict) or set(body) != names:
            return ReviewOutcome(failure="investigator runtime reply has the wrong shape")
        if body["failure"] is None and not isinstance(body["review"], dict):
            return ReviewOutcome(failure="investigator runtime reply has no review")
        return ReviewOutcome(**body)
    except Exception as exc:
        return ReviewOutcome(failure=f"investigator runtime error: {type(exc).__name__}")
