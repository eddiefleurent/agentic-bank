"""Entry points of the AML triage workflow on AWS (slice 3). Thin: wiring around existing code.

Two Lambdas, two roles:

  agentic-bank-aml         `handler`, dispatched on event["action"]:
    open    rules + snapshot (plain code) -> write partition CASE#<alert_id> -> start the workflow
    check   read the case again with its own credentials -> `triage.check` on the model's answer
    record  write the checked recommendation to the analyst queue (analyst_queue = "aml")
  agentic-bank-aml-review  `review_handler`: the one-call baseline. Reads ONLY its own case,
    through the case-reader role. That role allows GetItem and Query only on CASE#A-* partitions;
    the session policy still pins dynamodb:LeadingKeys to CASE#<alert_id>. No write rights
    anywhere; its only other right is the one model.

v1 is human-only: nothing here closes an alert, files a report or contacts the customer. A
`close` is advice an analyst reads; the queue row is the end of the workflow.
"""

import json
import os
import sys
import uuid
from datetime import UTC, datetime
from functools import cache
from typing import Any

import boto3

from agentic_bank.aml import data, rules, snapshot, triage
from agentic_bank.aml.case_tools import DynamoCaseSource
from agentic_bank.shared.data import READ, scoped_session

QUEUE = "aml"  # analyst_queue value; the dispute analyst queries "open", so the queues stay apart
RECOMMENDATION = "RECOMMENDATION"


def _table() -> str:
    return os.environ["TABLE_ARN"].rpartition("/")[2]


@cache
def _world() -> tuple[data.Dataset, dict[str, rules.Alert], list[rules.Alert]]:
    """The seeded synthetic bank and its alerts. Labels are generated alongside and dropped here:
    nothing on AWS ever sees them (the proof picks its alerts from labels on the caller's machine).
    A bank's rules engine reads the transaction store instead."""
    ds, _labels = data.generate()
    alerts = rules.run_rules(ds)
    return ds, {a.alert_id: a for a in alerts}, alerts


def open_case(alert_id: str, ddb: Any, sfn: Any) -> dict[str, Any]:
    key = snapshot.case_key(alert_id)
    ds, by_id, alerts = _world()
    if alert_id not in by_id:
        raise LookupError(f"no alert {alert_id}")
    items = snapshot.to_ddb(snapshot.build_case_snapshot(by_id[alert_id], ds, alerts))
    # PutItem one by one: the workload boundary allows PutItem, not BatchWriteItem. A replay
    # writes the same rows again (the data is deterministic).
    for item in items:
        ddb.put_item(TableName=_table(), Item=item)
    started = sfn.start_execution(
        stateMachineArn=os.environ["AML_STATE_MACHINE_ARN"],
        name=f"{alert_id}-{uuid.uuid4().hex[:8]}",
        input=json.dumps({"alert_id": alert_id}),
    )
    return {"case_id": key, "items": len(items), "execution_arn": started["executionArn"]}


def check_review(alert_id: str, review: dict[str, Any], ddb: Any) -> dict[str, Any]:
    """The code checks, on the case as THIS step read it, not as the reviewer reported it."""
    snap = DynamoCaseSource(ddb, _table(), alert_id).snapshot
    answer = (review or {}).get("answer")
    if answer is None:
        outcome = triage.failed((review or {}).get("failure") or "review returned no answer")
    else:
        outcome = triage.check_snapshot(answer, snap)
    return {
        "recommendation": outcome.recommendation,
        "failure": outcome.failure,
        "advice": outcome.advice,
    }


def record(alert_id: str, checked: dict[str, Any], execution: str, ddb: Any) -> dict[str, Any]:
    """One queue row per case. Status stays awaiting_analyst: only a person moves it on."""
    rec = checked.get("recommendation")
    if rec not in ("close", "escalate"):  # anything else is a bug upstream: a person looks
        checked = {"recommendation": "escalate", "failure": "bad check output", "advice": None}
    failure = checked.get("failure")
    ddb.put_item(
        TableName=_table(),
        Item={
            "customer": {"S": snapshot.case_key(alert_id)},
            "sk": {"S": RECOMMENDATION},
            "analyst_queue": {"S": QUEUE},
            "status": {"S": "awaiting_analyst"},
            "recommendation": {"S": checked["recommendation"]},
            "failure": {"S": failure} if failure else {"NULL": True},
            "advice": {"S": json.dumps(checked.get("advice"))},
            "model_id": {"S": os.environ.get("MODEL_ID", "")},
            "execution": {"S": execution},
            "created_at": {"S": datetime.now(UTC).isoformat(timespec="seconds")},
        },
    )
    return {"recommendation": checked["recommendation"], "failure": failure}


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    action, alert_id = event.get("action"), event["alert_id"]
    snapshot.case_key(alert_id)  # every action: the id's shape first
    ddb = boto3.client("dynamodb")
    if action == "open":
        return open_case(alert_id, ddb, boto3.client("stepfunctions"))
    if action == "check":
        return check_review(alert_id, event.get("review") or {}, ddb)
    if action == "record":
        return record(alert_id, event.get("checked") or {}, event.get("execution", ""), ddb)
    raise ValueError(f"unknown action {action!r}")


def case_reader(alert_id: str) -> Any:
    """A DynamoDB client that can read CASE#<alert_id> and nothing else (AWS enforces it)."""
    key = snapshot.case_key(alert_id)
    session = scoped_session(
        "CASE_READER_ROLE_ARN",
        key,
        os.environ["TABLE_ARN"],
        READ,
        session_name=f"aml-review-{alert_id}",
    )
    return session.client("dynamodb")


def _audit_model_call(
    alert_id: str, model_id: str, snap: dict[str, Any], run: triage.TriageRun
) -> None:
    """One line per model call. The hash stands in for the prompt: the evidence pack stays out."""
    called = run.usage.get("model_calls") == 1 or (run.outcome.failure or "").startswith(
        "baseline error"
    )
    if not called:  # over budget returns before Converse; there is no call to record
        return
    line = {
        "event": "aml.review.audit",
        "alert_id": alert_id,
        "case_id": snapshot.case_key(alert_id),
        "model_id": model_id,
        "input_sha256": triage.prompt_evidence_sha256(snap),
        "recommendation": run.outcome.recommendation,
        "check": "pass" if run.outcome.failure is None else "fail",
    }
    print(json.dumps(line), file=sys.stderr)


def review_handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """The Review step: read the one case, one model call, return the raw answer for `check`."""
    alert_id = event["alert_id"]
    snap = DynamoCaseSource(case_reader(alert_id), _table(), alert_id).snapshot
    model_id = os.environ["MODEL_ID"]
    run, answer = triage.call_baseline(boto3.client("bedrock-runtime"), model_id, snap)
    _audit_model_call(alert_id, model_id, snap, run)
    return {
        "answer": answer,
        "failure": None if answer is not None else run.outcome.failure,
        "usage": run.usage,
        "stop_reason": run.stop_reason,
        "seconds": run.seconds,
    }
