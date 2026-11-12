"""Proof: AWS, not our code, stops one customer's session from reading another's partition.

    uv run python proofs/iam_session_policy.py

Assumes the data-reader role with the same session policy the tools Lambda builds for alex, then
calls DynamoDB directly (boto3) for alex's and sam's partitions. Needs credentials allowed to
assume the role (its trust policy must permit the caller) and read Cognito/DynamoDB metadata.
"""

import sys
from pathlib import Path

import boto3
from _common import REGION, Stack, check, run
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agentic_bank.shared.data import session_policy  # noqa: E402  the real policy builder

TABLE = "agentic-bank-data"
ROLE = "agentic-bank-app-data-reader"


def denied(call) -> bool:
    try:
        call()
    except ClientError as error:
        return error.response["Error"]["Code"] == "AccessDeniedException"
    return False


def main() -> None:
    stack = Stack()
    alex, sam = stack.subject("alex"), stack.subject("sam")
    account = boto3.client("sts").get_caller_identity()["Account"]
    table_arn = boto3.client("dynamodb", region_name=REGION).describe_table(TableName=TABLE)[
        "Table"
    ]["TableArn"]
    creds = boto3.client("sts").assume_role(
        RoleArn=f"arn:aws:iam::{account}:role/{ROLE}",
        RoleSessionName="proof-alex",
        Policy=session_policy(table_arn, alex),
    )["Credentials"]
    ddb = boto3.client(
        "dynamodb",
        region_name=REGION,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )

    def query(customer: str):
        return lambda: ddb.query(
            TableName=TABLE,
            KeyConditionExpression="customer = :c",
            ExpressionAttributeValues={":c": {"S": customer}},
        )

    check("session for alex: Query on alex's partition succeeds", bool(query(alex)()["Items"]))
    check("session for alex: Query on sam's partition is AccessDenied", denied(query(sam)))
    check(
        "session for alex: GetItem on sam's account is AccessDenied",
        denied(
            lambda: ddb.get_item(
                TableName=TABLE, Key={"customer": {"S": sam}, "sk": {"S": "ACCT#acc_sam_chk"}}
            )
        ),
    )
    check("session for alex: Scan is AccessDenied", denied(lambda: ddb.scan(TableName=TABLE)))


if __name__ == "__main__":
    run(main)
