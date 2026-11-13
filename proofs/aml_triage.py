"""Proof: AML triage v1 on AWS, end to end, and the wall around each case.

    uv run python proofs/aml_triage.py

1. One seeded suspicious alert and one benign look-alike, picked HERE from the eval labels (the
   labels never go to AWS). For each: invoke agentic-bank-aml {action: open} (rules + snapshot ->
   partition CASE#<alert_id> -> start agentic-bank-aml-triage), wait for the execution, read the
   RECOMMENDATION row. Suspicious must be escalate; both must wait for a person (never closed).
2. Deliberate failure: the reviewer's own credential builder (`handler.case_reader`, the data-reader
   role with a session policy for ONE case) reads its case, then tries the other case, a Scan and a
   write. AWS must answer AccessDenied to all three.
3. Cleanup: every row of both CASE# partitions is deleted (admin credentials), pass or fail.

Needs admin credentials allowed to invoke Lambda, read Step Functions, assume the data-reader role
(its trust allows the account) and delete items. Two gpt-oss calls: under one cent.
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import boto3
from _common import REGION, check, run
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
from agentic_bank.aml import handler  # noqa: E402  the real reviewer credential builder
from evals.aml import harness  # noqa: E402  labels stay on this machine

TABLE = "agentic-bank-data"
FUNCTION = "agentic-bank-aml"
ROLE = "agentic-bank-app-data-reader"
# Both were judged right by the baseline in the slice 2 eval run (evals/aml/results/baseline.md).


def denied(call: Any) -> bool:
    try:
        call()
    except ClientError as error:
        return error.response["Error"]["Code"] == "AccessDeniedException"
    return False


def wait(sfn: Any, arn: str, seconds: int = 240) -> str:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        status = sfn.describe_execution(executionArn=arn)["status"]
        if status != "RUNNING":
            return status
        time.sleep(3)
    return "STILL_RUNNING"


def triage_one(lam: Any, sfn: Any, ddb: Any, alert_id: str) -> dict[str, Any]:
    reply = lam.invoke(
        FunctionName=FUNCTION, Payload=json.dumps({"action": "open", "alert_id": alert_id})
    )
    body = json.loads(reply["Payload"].read())
    if "FunctionError" in reply:
        print(f"      open failed: {body.get('errorType')}: {body.get('errorMessage')}")
        return {}
    status = wait(sfn, body["execution_arn"])
    check(
        f"{alert_id}: workflow {status} ({body['items']} case rows written)", status == "SUCCEEDED"
    )
    row = ddb.get_item(
        TableName=TABLE,
        Key={"customer": {"S": f"CASE#{alert_id}"}, "sk": {"S": "RECOMMENDATION"}},
        ConsistentRead=True,
    ).get("Item", {})
    return {k: next(iter(v.values())) for k, v in row.items()}


def cleanup(ddb: Any, alert_ids: list[str]) -> None:
    for alert_id in alert_ids:
        args: dict[str, Any] = {
            "TableName": TABLE,
            "KeyConditionExpression": "customer = :c",
            "ExpressionAttributeValues": {":c": {"S": f"CASE#{alert_id}"}},
            "ProjectionExpression": "customer, sk",
        }
        n = 0
        while True:
            page = ddb.query(**args)
            for key in page["Items"]:
                ddb.delete_item(TableName=TABLE, Key=key)
                n += 1
            if "LastEvaluatedKey" not in page:
                break
            args["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        print(f"      cleanup: {n} rows deleted from CASE#{alert_id}")


def main() -> None:
    cases, _, _ = harness.load_cases()
    # First suspicious and first benign case in label order (label ids carry no verdict hint).
    sus = next(c for c in cases if c.verdict == "suspicious" and c.alert is not None)
    ben = next(c for c in cases if c.verdict == "benign" and c.alert is not None)
    assert sus.verdict == "suspicious" and ben.verdict == "benign"
    alerts = [sus.alert.alert_id, ben.alert.alert_id]

    lam = boto3.client("lambda", region_name=REGION)
    sfn = boto3.client("stepfunctions", region_name=REGION)
    ddb = boto3.client("dynamodb", region_name=REGION)
    try:
        rec = triage_one(lam, sfn, ddb, sus.alert.alert_id)
        check(
            f"suspicious {sus.alert.alert_id}: recommendation escalate "
            f"(got {rec.get('recommendation')}, failure {rec.get('failure')})",
            rec.get("recommendation") == "escalate",
        )
        check("suspicious: waits for a person", rec.get("status") == "awaiting_analyst")
        rec = triage_one(lam, sfn, ddb, ben.alert.alert_id)
        # Advice only: a benign look-alike may still be escalated. What must hold is the queue.
        print(f"      benign {ben.alert.alert_id}: model advice {rec.get('recommendation')}")
        check(
            "benign: in the AML queue, waiting for a person, not closed",
            rec.get("analyst_queue") == "aml" and rec.get("status") == "awaiting_analyst",
        )

        # The reviewer's credentials, built by the same function the review Lambda calls.
        table_arn = ddb.describe_table(TableName=TABLE)["Table"]["TableArn"]
        account = table_arn.split(":")[4]
        os.environ["TABLE_ARN"] = table_arn
        os.environ["DATA_READER_ROLE_ARN"] = f"arn:aws:iam::{account}:role/{ROLE}"
        os.environ.setdefault("AWS_DEFAULT_REGION", REGION)
        reader = handler.case_reader(sus.alert.alert_id)

        def query(alert_id: str) -> Any:
            return lambda: reader.query(
                TableName=TABLE,
                KeyConditionExpression="customer = :c",
                ExpressionAttributeValues={":c": {"S": f"CASE#{alert_id}"}},
            )

        own = query(sus.alert.alert_id)()["Items"]
        check(f"reviewer for {sus.alert.alert_id}: reads its own case", bool(own))
        check(
            f"reviewer for {sus.alert.alert_id}: CASE#{ben.alert.alert_id} is AccessDenied",
            denied(query(ben.alert.alert_id)),
        )
        check("reviewer: Scan is AccessDenied", denied(lambda: reader.scan(TableName=TABLE)))
        check(
            "reviewer: writing its own case is AccessDenied",
            denied(
                lambda: reader.put_item(
                    TableName=TABLE,
                    Item={
                        "customer": {"S": f"CASE#{sus.alert.alert_id}"},
                        "sk": {"S": "RECOMMENDATION"},
                        "recommendation": {"S": "close"},
                    },
                )
            ),
        )
    finally:
        cleanup(ddb, alerts)


if __name__ == "__main__":
    run(main)
