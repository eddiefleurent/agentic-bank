"""AgentCore Runtime entrypoint: the investigator as a hosted agent (planned, not deployed).

AgentCore Runtime runs this file (direct code deployment, arm64, PYTHON_3_14) in a per-session
microVM and calls POST /invocations with the payload the review Lambda sends:
{"case_file": {...}, "customer": "alex", "case_id": "dsp_t_alex_006"}. The reply is a ReviewOutcome
as JSON, the same shape the classifier Lambda returns, so the workflow's Decide step is unchanged.

Local check:  uv run python -m agentic_bank.investigator.runtime   (serves on :8080)
"""

import dataclasses
import os
import re
from typing import Any

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from agentic_bank.disputes.review import ReviewOutcome
from agentic_bank.investigator.agent import run_investigation
from agentic_bank.investigator.evidence import HistorySource, TableHistory

ID = re.compile(r"^[a-z0-9_]{1,64}$")
app = BedrockAgentCoreApp()


def handle(payload: Any, history_for: Any = TableHistory.for_case) -> dict[str, Any]:
    """Validate the payload, then investigate. Never raises: errors are failure markers."""
    if not isinstance(payload, dict) or not isinstance(payload.get("case_file"), dict):
        return dataclasses.asdict(ReviewOutcome(failure="bad request: case_file missing"))
    customer, case_id = payload.get("customer"), payload.get("case_id")
    history: HistorySource | None = None
    if isinstance(customer, str) and isinstance(case_id, str):
        if not (ID.fullmatch(customer) and ID.fullmatch(case_id)):
            return dataclasses.asdict(ReviewOutcome(failure="bad request: ids"))
        try:
            history = history_for(customer, case_id)  # read-only, this customer's rows only
        except Exception:
            history = None  # history is context, not a fact code decides on: carry on without
    outcome = run_investigation(
        boto3.client("bedrock-runtime"),
        os.environ["MODEL_ID"],
        os.environ["GUARDRAIL_ID"],
        os.environ["GUARDRAIL_VERSION"],
        payload["case_file"],
        history=history,
    )
    return dataclasses.asdict(outcome)


@app.entrypoint
def invoke(payload: dict[str, Any]) -> dict[str, Any]:
    return handle(payload)


if __name__ == "__main__":
    app.run()
