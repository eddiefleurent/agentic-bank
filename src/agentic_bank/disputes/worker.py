"""Entry points the dispute state machine calls. Thin: each action wires the pure logic to DynamoDB.

`handler` (Lambda agentic-bank-dispute, the only money writer) dispatches on event["action"]:
build, decide, ask, to_analyst, settle, close. `review_handler` (Lambda agentic-bank-review) holds
only model and guardrail permissions and never touches the table. All inputs are {customer, case_id}
plus what the previous state returned; the case row, not the workflow, is the source of truth.
"""

import dataclasses
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import boto3

from agentic_bank.disputes import casefile, decide, questions, review, settle
from agentic_bank.network.order_insight import FixtureOrderInsight
from agentic_bank.shared.data import scoped_session
from agentic_bank.shared.ddb import deser, get
from agentic_bank.shared.ddb import ser as _ser

WRITES = [
    "dynamodb:GetItem",
    "dynamodb:Query",
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:ConditionCheckItem",
]


def writer_client(customer: str) -> Any:
    """Low-level DynamoDB client that can only touch `customer`'s partition (read and write)."""
    arn = os.environ["TABLE_ARN"]
    return scoped_session("DATA_WRITER_ROLE_ARN", customer, arn, WRITES).client("dynamodb")


def find_txn(client: Any, table: str, customer: str, case: dict[str, Any]) -> dict[str, Any]:
    """The disputed TXN row. Its sort key holds the date, so match on the trailing id."""
    prefix = f"TXN#{case['account_id']}#"
    kwargs: dict[str, Any] = {
        "TableName": table,
        "KeyConditionExpression": "customer = :c AND begins_with(sk, :p)",
        "ExpressionAttributeValues": {":c": _ser(customer), ":p": _ser(prefix)},
    }
    while True:
        page = client.query(**kwargs)
        for item in page["Items"]:
            if item["sk"]["S"].endswith(f"#{case['transaction_id']}"):
                return deser(item)
        if "LastEvaluatedKey" not in page:
            raise LookupError("transaction not found")
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _case(client: Any, table: str, customer: str, case_id: str) -> dict[str, Any]:
    row = get(client, table, customer, f"CASE#{case_id}")
    if row is None:
        raise LookupError("no such case")
    return row


def _plain(value: Any) -> Any:
    """Decimals (from DynamoDB) to ints, so Step Functions can carry them as JSON."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _update(client: Any, table: str, customer: str, case_id: str, expr: str, **kw: Any) -> None:
    client.update_item(
        TableName=table,
        Key={"customer": _ser(customer), "sk": _ser(f"CASE#{case_id}")},
        UpdateExpression=expr,
        ExpressionAttributeNames={"#s": "status"},
        **kw,
    )


def build(client: Any, table: str, customer: str, case_id: str) -> dict[str, Any]:
    case = _plain(_case(client, table, customer, case_id))  # DynamoDB numbers are Decimals
    txn = find_txn(client, table, customer, case)
    ref = txn.get("network_ref")
    order = FixtureOrderInsight().lookup(ref) if ref else None
    case_file = casefile.build_case_file(case, order)
    return _plain(
        {
            "case_file": case_file,
            "sha256": casefile.case_file_sha256(case_file),
            "code_scores": casefile.code_scores(case["reason_code"], order),
            "has_order": order is not None,
            "reason_code": case["reason_code"],
            "amount_cents": case["amount_cents"],
            "round": case.get("round", 0),
        }
    )


def decide_case(
    client: Any, table: str, customer: str, case_id: str, built: dict[str, Any], outcome: dict
) -> dict[str, Any]:
    d = decide.decide(
        reason_code=built["reason_code"],
        amount_cents=built["amount_cents"],
        has_order=built["has_order"],
        code_scores=built["code_scores"],
        review=review.ReviewOutcome(**outcome),
        round_no=built["round"],
    )
    record = {"rule": d.rule, "outcome": d.outcome, "grounding": outcome.get("grounding")}
    _update(
        client,
        table,
        customer,
        case_id,
        "SET decision = :d, case_file_sha256 = :h",
        ConditionExpression="#s = :rev",  # decided only while under review
        ExpressionAttributeValues={
            ":rev": _ser("REVIEWING"),
            ":d": _ser(json.loads(json.dumps(record), parse_float=Decimal)),
            ":h": _ser(built["sha256"]),
        },
    )
    return dataclasses.asdict(d)


def ask(client: Any, table: str, customer: str, case_id: str, question_id: str, token: str) -> None:
    """Called with .waitForTaskToken: store the question and the token, then return. The answer
    tool resumes the execution (waits.resume)."""
    questions.question_text(question_id)  # fail fast on an unknown id
    _update(
        client,
        table,
        customer,
        case_id,
        "SET #s = :ask, question_id = :q, task_token = :t",
        ConditionExpression="#s = :rev",
        ExpressionAttributeValues={
            ":ask": _ser("INFO_REQUESTED"),
            ":rev": _ser("REVIEWING"),
            ":q": _ser(question_id),
            ":t": _ser(token),
        },
    )


def to_analyst(client: Any, table: str, customer: str, case_id: str, token: str) -> None:
    """Called with .waitForTaskToken (1 h): open the case in the analyst queue.

    Still INFO_REQUESTED here means the customer never answered (an answer sets REVIEWING), so
    the stored decision becomes rule 8, conditional on that same status."""
    timed_out = _case(client, table, customer, case_id)["status"] == "INFO_REQUESTED"
    values = {":a": _ser("ANALYST_REVIEW"), ":open": _ser("open"), ":t": _ser(token)}
    if timed_out:
        rule8 = decide.TIMEOUT
        values |= {
            ":ask": _ser("INFO_REQUESTED"),
            ":d": _ser({"rule": rule8.rule, "outcome": rule8.outcome}),
        }
        expr, cond = ", decision = :d", "#s = :ask"
    else:
        values |= {":rev": _ser("REVIEWING")}
        expr, cond = "", "#s = :rev"
    _update(
        client,
        table,
        customer,
        case_id,
        f"SET #s = :a, analyst_queue = :open, task_token = :t{expr} REMOVE question_id",
        ConditionExpression=cond,
        ExpressionAttributeValues=values,
    )


def close(client: Any, table: str, customer: str, case_id: str, decided_by: str) -> dict[str, Any]:
    """A person said no. Only reachable from ANALYST_REVIEW."""
    _update(
        client,
        table,
        customer,
        case_id,
        "SET #s = :c, decided_by = :by REMOVE analyst_queue, task_token",
        ConditionExpression="#s = :a",
        ExpressionAttributeValues={
            ":c": _ser("CLOSED_NO_REFUND"),
            ":a": _ser("ANALYST_REVIEW"),
            ":by": _ser(decided_by),
        },
    )
    return {"closed": True}


def handler(event: dict[str, Any], _context: Any = None) -> Any:
    table = os.environ["TABLE_ARN"].rpartition("/")[2]
    customer, case_id = event["customer"], event["case_id"]
    client = writer_client(customer)
    match event["action"]:
        case "build":
            return build(client, table, customer, case_id)
        case "decide":
            return decide_case(client, table, customer, case_id, event["built"], event["review"])
        case "ask":
            return ask(client, table, customer, case_id, event["question_id"], event["task_token"])
        case "to_analyst":
            return to_analyst(client, table, customer, case_id, event["task_token"])
        case "settle":
            case = _case(client, table, customer, case_id)
            txn = find_txn(client, table, customer, case)
            # A staff refund needs the analyst's reason on the case. Only the analyst function can
            # write it, so a task token replayed by anything else cannot settle.
            if not event["auto"] and not case.get("analyst_reason"):
                raise settle.SettleRefused("no analyst decision on the case")
            return settle.settle(
                client,
                table,
                customer,
                case,
                txn,
                auto=event["auto"],
                decided_by=event["decided_by"],
                now=datetime.now(UTC),
            )
        case "close":
            return close(client, table, customer, case_id, event["decided_by"])
    raise ValueError(f"unknown action {event['action']!r}")


def review_handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    outcome = review.run_review(
        boto3.client("bedrock-runtime"),
        os.environ["MODEL_ID"],
        os.environ["GUARDRAIL_ID"],
        os.environ["GUARDRAIL_VERSION"],
        event["case_file"],
    )
    return dataclasses.asdict(outcome)
