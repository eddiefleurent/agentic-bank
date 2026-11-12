"""Proof: AWS, not our code, limits what the agent-side (proposer) credentials can write.

    uv run python proofs/money_wall.py

Assumes `agentic-bank-app-proposer` with the same session policy the tools Lambda builds for alex,
then calls DynamoDB directly. Needs credentials the role's trust policy accepts (the owner's admin
session; a bank would use a break-glass role) and the deployed stack. Writes one XFER item and
removes it with the admin credentials at the end. Also exercises the case-writer role with
and without a session policy, using only a disposable case key. The final role-assumption
checks are IAM simulations, not live STS attempts from a tools Lambda session.
"""

import sys
from pathlib import Path

import boto3
from _common import REGION, Stack, check, run
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agentic_bank.shared.data import session_policy  # noqa: E402  the real policy builder
from agentic_bank.tools.disputes import CASE_ATTRIBUTES  # noqa: E402

TABLE = "agentic-bank-data"
ROLE = "agentic-bank-app-proposer"
PROBE = "XFER#xfer_0000000000wall0"


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
    admin = boto3.client("dynamodb", region_name=REGION)
    table_arn = admin.describe_table(TableName=TABLE)["Table"]["TableArn"]
    creds = boto3.client("sts").assume_role(
        RoleArn=f"arn:aws:iam::{account}:role/{ROLE}",
        RoleSessionName="proof-alex",
        Policy=session_policy(table_arn, alex, ["dynamodb:PutItem"]),
    )["Credentials"]
    ddb = boto3.client(
        "dynamodb",
        region_name=REGION,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )

    def put(customer: str, sk: str, **attributes: dict[str, str]):
        item = {"customer": {"S": customer}, "sk": {"S": sk}} | attributes
        return lambda: ddb.put_item(TableName=TABLE, Item=item)

    xfer_fields = {
        "from_account_id": {"S": "acc_alex_chk"},
        "payee_id": {"S": "pay_alex_landlord"},
        "amount_cents": {"N": "100"},
        "memo": {"S": ""},
        "status": {"S": "PENDING"},
        "created_at": {"S": "2026-11-12T10:00:00Z"},
        "expires_at": {"S": "2026-11-12T10:30:00Z"},
        "request_hash": {"S": "0" * 64},
    }
    try:
        put(alex, PROBE, **xfer_fields)()
        allowed = True
    except ClientError:
        allowed = False
    check("proposer session for alex: PutItem of an XFER item is allowed", allowed)
    try:
        check(
            "proposer session: PutItem setting balance_cents is AccessDenied",
            denied(put(alex, "ACCT#acc_alex_chk", balance_cents={"N": "99999999"})),
        )
        check(
            "proposer session: PutItem of a TXN row (description) is AccessDenied",
            denied(
                put(
                    alex,
                    "TXN#acc_alex_chk#2026-11-12#t_wall",
                    description={"S": "ACH TRANSFER TO ME"},
                    amount_cents={"N": "-100"},
                )
            ),
        )
        check(
            "proposer session: UpdateItem (approve) is AccessDenied",
            denied(
                lambda: ddb.update_item(
                    TableName=TABLE,
                    Key={"customer": {"S": alex}, "sk": {"S": PROBE}},
                    UpdateExpression="SET #s = :e",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":e": {"S": "EXECUTED"}},
                )
            ),
        )
        check(
            "alex's proposer session: PutItem in sam's partition is AccessDenied",
            denied(put(sam, PROBE, **xfer_fields)),
        )
    finally:
        admin.delete_item(TableName=TABLE, Key={"customer": {"S": alex}, "sk": {"S": PROBE}})

    # The role policy enforces case attributes even when the caller omits its session policy.
    creds = boto3.client("sts").assume_role(
        RoleArn=f"arn:aws:iam::{account}:role/agentic-bank-app-case-writer",
        RoleSessionName="proof-alex-cases",
        Policy=session_policy(
            table_arn, alex, ["dynamodb:PutItem", "dynamodb:UpdateItem"], CASE_ATTRIBUTES
        ),
    )["Credentials"]
    case_writer = boto3.client(
        "dynamodb",
        region_name=REGION,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )
    check(
        "case-writer session: UpdateItem of balance_cents is AccessDenied",
        denied(
            lambda: case_writer.update_item(
                TableName=TABLE,
                Key={"customer": {"S": alex}, "sk": {"S": "CASE#dsp_iam_probe"}},
                UpdateExpression="SET balance_cents = balance_cents + :c",
                ExpressionAttributeValues={":c": {"N": "100000"}},
            )
        ),
    )
    check(
        "case-writer session: PutItem of a card charge (channel, description) is AccessDenied",
        denied(
            lambda: case_writer.put_item(
                TableName=TABLE,
                Item={
                    "customer": {"S": alex},
                    "sk": {"S": "CASE#dsp_iam_probe"},
                    "description": {"S": "FORGED"},
                    "channel": {"S": "card"},
                    "amount_cents": {"N": "-5000"},
                },
            )
        ),
    )

    # Deliberately omit Policy: a compromised tools process can omit our session restriction too.
    creds = boto3.client("sts").assume_role(
        RoleArn=f"arn:aws:iam::{account}:role/agentic-bank-app-case-writer",
        RoleSessionName="proof-cases-no-policy",
    )["Credentials"]
    bare = boto3.client(
        "dynamodb",
        region_name=REGION,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )
    probe_key = {"customer": {"S": alex}, "sk": {"S": "CASE#dsp_iam_probe"}}
    try:
        bare.put_item(TableName=TABLE, Item=probe_key | {"status": {"S": "REVIEWING"}})
        check("case writer WITHOUT session policy: case PutItem allowed", True)
        bare.update_item(
            TableName=TABLE,
            Key=probe_key,
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": {"S": "INFO_REQUESTED"}},
        )
        check("case writer WITHOUT session policy: case UpdateItem allowed", True)
        for attribute, value in {
            "balance_cents": {"N": "999999"},
            "description": {"S": "FORGED"},
            "channel": {"S": "card"},
        }.items():
            check(
                f"case writer WITHOUT session policy: PutItem {attribute} denied",
                denied(
                    lambda a=attribute, v=value: bare.put_item(
                        TableName=TABLE,
                        Item=probe_key | {a: v},
                    )
                ),
            )
            check(
                f"case writer WITHOUT session policy: UpdateItem {attribute} denied",
                denied(
                    lambda a=attribute, v=value: bare.update_item(
                        TableName=TABLE,
                        Key=probe_key,
                        UpdateExpression="SET #a = :v",
                        ExpressionAttributeNames={"#a": a},
                        ExpressionAttributeValues={":v": v},
                    )
                ),
            )
    finally:
        admin.delete_item(TableName=TABLE, Key=probe_key)
    # Read-only simulation of the real tools identity (identity + workload boundary).
    iam = boto3.client("iam")
    for role in ["data-writer", "approver"]:
        result = iam.simulate_principal_policy(
            PolicySourceArn=f"arn:aws:iam::{account}:role/agentic-bank-app-tools",
            ActionNames=["sts:AssumeRole"],
            ResourceArns=[f"arn:aws:iam::{account}:role/agentic-bank-app-{role}"],
        )["EvaluationResults"]
        check(
            f"tools identity simulation: cannot assume {role}",
            all(r["EvalDecision"] != "allowed" for r in result),
        )


if __name__ == "__main__":
    run(main)
